#!/usr/bin/env python3
"""Economy chain — payments: product facts → Creem proof → fulfillment,
refund/dispute → exact reversal, the /api/topup route contract, and
the payment audit closure.
"""
from _harness import PG, REPO, TEST_PORT, check, finish, purge_users, section  # fail-closed gate runs on import


# ════════════════════════════════════════════════════════════════════════
# Payment audit closure
#
# the payment audit closure — behavior tests over real paths.
#
# T*  template escaping: /tools?q="><script> and a content_type-carrying
#     upload render ESCAPED text (autoescape ON; no |safe for user data).
# S*  semaphore: 9 concurrent tools/call invocations all complete.
# U*  upgrade refund with a LOST upgrade callback: only the upgrade
#     credits revoke; the base-paid membership survives.
# K*  ticket hash storage: same-ref retry invalidates the old credential
#     (fresh upload_url); replay PUT response carries expires_at.
# F*  unified snake_case items: execute response, MCP structuredContent
#     and the /r record share one item shape; storage ids absent.
# ════════════════════════════════════════════════════════════════════════
section('Payment audit closure')

import base64
import http.client as _hc
import json as _json
import secrets as _secrets
import sys as _sys
import time
import urllib.parse
import uuid


_HOST = ("127.0.0.1", int(TEST_PORT))
_BASE = f"http://127.0.0.1:{TEST_PORT}"
_u = "fx2" + uuid.uuid4().hex[:8]

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAIAAAACUFjqAAAAPElEQVR4nGM8YWPDgBsw4ZFjYGBggV"
    "A+N7agSWzR8EFIw/loqlEMNz98GJ/dJ21tyXIaVtcxMDAwUuRvAGHKDXGPxOJMAAAAAElFTkSuQmCC")


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=60)
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else _json.dumps(body).encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    setc = r.getheader("Set-Cookie") or ""
    conn.close()
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


def _mcp(body, token):
    st, _h, d, _sc = _req("POST", "/mcp", body, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})
    return st, d


def _call(name, arguments, token, mid=1):
    return _mcp({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token)


uid = None
TOK = None
CK = None
created = []
from database import db  # noqa: E402
from utils import now  # noqa: E402

ts = now()
try:
    # ── fixtures ──
    b, ct = _form({"username": _u + "a", "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{_u}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": _u + "a", "password": "Test12345"})
    st, _h, _d, sc = _req("POST", "/auth/login", b, {"Content-Type": ct})
    CK = sc.split(";")[0]
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (_u + "a",)).fetchone()["id"])
        created.append(uid)
        from repo.ledger import insert_row
        insert_row(con, uid, 100000, 100000, "manual_topup", None, None,
                   f"fx2:{_u}", None, ts)
    st, _h, d_k, _sc = _req("POST", "/api/keys", {"name": "fx2"},
                            {"Content-Type": "application/json", "Cookie": CK})
    TOK = d_k["key"]["key"]
    check("F1 user + key + credits", bool(TOK), TOK)

    # ═══ T: template escaping over real pages ═══
    st, _h, body, _sc = _req("GET", "/tools?q=" + urllib.parse.quote('"><script>alert(1)</script>'))
    check("T1 /tools?q= injection renders escaped",
          st == 200 and "<script>alert(1)</script>" not in body
          and ("&lt;script&gt;" in body or "&amp;gt;" in body or "script" not in body),
          str(body)[:0] + ("found raw script" if isinstance(body, str) and "<script>alert" in body else "ok"))
    check("T1b page still renders (200 + tools markup)",
          st == 200 and ("tool" in body.lower() or "market" in body.lower()), st)
    # upload with a hostile content_type-ish filename via share tool
    A64 = base64.b64encode(PNG).decode()
    st, d = _call("share", {"files": [{"filename": 'x"><script>.png',
                                       "b64": A64}],
                            "client_ref": f"t2-{_u}"}, TOK, 10)
    res = d.get("result") or {}
    items = (res.get("structuredContent") or {}).get("items") or []
    fitem = next((i for i in items if i.get("kind") == "file"), {})
    check("T2 share with hostile filename succeeds (name sanitized)",
          res.get("isError") is False and fitem.get("filename"), str(d)[:100])
    fn = fitem.get("filename") or ""
    check("T2b stored filename carries no raw markup",
          "<script>" not in fn and '"' not in fn, fn)
    if fitem.get("page_url"):
        pid = fitem["page_url"].rsplit("/f/", 1)[-1]
        st, _h, fbody, _sc = _req("GET", f"/f/{pid}")
        check("T3 /f page renders the hostile name escaped",
              st == 200 and "<script>alert(1)</script>" not in fbody, st)
    # source upload with hostile content_type in the /source fetch is not
    # user-reachable (signed URLs); the file NAME path covers the page.

    # ═══ S: 9 concurrent calls all complete ═══
    import concurrent.futures as _cf
    def _one_hosted(i):
        st, d = _call("compress", {"files": [{"filename": f"s{i}.png",
                                              "b64": A64}],
                                   "client_ref": f"s9-{_u}-{i}"}, TOK, 20 + i)
        res = d.get("result") or {}
        return res.get("isError") is False
    with _cf.ThreadPoolExecutor(max_workers=9) as _ex:
        oks = list(_ex.map(_one_hosted, range(9)))
    check("S1 9 concurrent calls all complete",
          all(oks), str(oks))

    # ═══ U: upgrade refund with a LOST upgrade callback ═══
    # fixtures: membership + base grant for the period from ANOTHER txn,
    # upgrade txn with NO grant row (callback lost), then reverse it.
    with db() as con:
        sub_id = f"sub_fx2_{_u}"
        con.execute(
            "insert into orders (user_id, kind, item_id, provider,"
            " provider_order_id, status, amount_cents, currency, paid_at,"
            " created_at) values (%s,'plan',1,'creem',%s,'paid',500,'USD',"
            " %s,%s)",
            (uid, f"sub:{sub_id}", ts, ts))
        con.execute(
            "insert into memberships (user_id, plan_id,"
            " provider_subscription_id, status, started_at, renews_at,"
            " last_grant_period_start, cancel_scheduled_at)"
            " values (%s,1,%s,'active',%s,%s,%s,NULL)"
            " on conflict (provider_subscription_id) do update set"
            " status='active', last_grant_period_start=%s",
            (uid, sub_id, ts - 86400, ts + 15 * 86400, ts, ts))
    # base grant from the BASE transaction — AFTER the fixture commit:
    # grant's own connection locks the user row FOR UPDATE, which the
    # FK inserts' KEY SHARE in the open transaction above would block
    from economy.billing import grant
    grant(uid, 500, "grant_plan", ref_type="creem_transaction",
          ref_id="txn_base_" + _u, idem_key=f"plan:{uid}:{ts}",
          note="base cycle")
    # the period's base payment is the recorded fact reverse() classifies by
    with db() as con:
        con.execute(
            "insert into subscription_period_payments (sub_id, period_start,"
            " user_id, base_txn_id, created_at) values (%s,%s,%s,%s,%s)",
            (sub_id, ts, uid, "txn_base_" + _u, ts))
    from economy import reverse as _rev
    _upg_txn = {"id": "txn_upg_" + _u, "subscription": sub_id,
                "status": "refunded", "type": "invoice",
                "period_start": ts, "period_end": ts + 30 * 86400}
    import economy.reconcile as _rec
    _orig_fetch = _rec.fetch_transaction
    _rec.fetch_transaction = lambda tid: _upg_txn if tid == _upg_txn["id"] else _orig_fetch(tid)
    try:
        out = _rev.reverse_payment("refund.created",
                                   {"id": "evt_" + _u,
                                    "object": {"object": "refund",
                                               "id": "ref_" + _u,
                                               "transaction": {"id": _upg_txn["id"]}}})
    finally:
        _rec.fetch_transaction = _orig_fetch
    with db() as con:
        m = con.execute("select status, last_grant_period_start from"
                        " memberships where provider_subscription_id=%s",
                        (sub_id,)).fetchone()
    check("U1 lost-callback upgrade refund keeps the base-paid membership",
          m is not None and m["status"] == "active", str(m))
    check("U2 zero credits revoked (nothing was granted for the upgrade)",
          out.get("credits") == 0 and out.get("kind") == "upgrade", str(out))

    # ═══ K: ticket hash + retry invalidation + replay expires_at ═══
    REF = f"k-{_u}"
    def _cu():
        st, d = _call("create_upload",
                      {"filename": "k.png", "size": len(PNG),
                       "client_ref": REF}, TOK, _secrets.randbelow(10**6))
        sc = (d.get("result") or {}).get("structuredContent") or {}
        return sc.get("upload_url")
    u1 = _cu()
    u2 = _cu()
    check("K1 same-ref retry issues a FRESH credential (old invalidated)",
          u1 and u2 and u1 != u2, (u1, u2))
    t2 = u2.rsplit("/", 1)[-1]
    st, _h, d_ok, _sc = _req("PUT", f"/mcp/upload/{t2}", PNG, {})
    check("K2 fresh credential PUT succeeds", st == 200 and d_ok.get("public_id"),
          f"{st} {str(d_ok)[:80]}")
    pid0, tok0 = d_ok.get("public_id"), d_ok.get("source_token")
    exp0 = d_ok.get("expires_at")
    check("K2b first PUT response carries expires_at",
          isinstance(exp0, int) and exp0 > ts, exp0)
    # old credential is dead
    t1 = u1.rsplit("/", 1)[-1]
    st, _h, d_old, _sc = _req("PUT", f"/mcp/upload/{t1}", PNG, {})
    check("K3 invalidated credential rejected", st in (404, 409, 410), st)
    # replay: same ref again + PUT → same handles + expires_at
    u3 = _cu()
    t3 = u3.rsplit("/", 1)[-1]
    st, _h, d_rp, _sc = _req("PUT", f"/mcp/upload/{t3}", PNG, {})
    check("K4 replay returns the original handles + expires_at",
          st == 200 and d_rp.get("public_id") == pid0
          and d_rp.get("source_token") == tok0
          and isinstance(d_rp.get("expires_at"), int),
          f"{st} {str(d_rp)[:90]}")

    # ═══ F: unified snake_case item shape across the three surfaces ═══
    st, d = _call("compress", {"files": [{"filename": "f.png", "b64": A64}],
                               "client_ref": f"f1-{_u}"}, TOK, 40)
    res = d.get("result") or {}
    sc_it = ((res.get("structuredContent") or {}).get("items") or [{}])[0]
    check("F1 MCP structuredContent item is snake_case + no storage ids",
          {"kind", "filename", "content_type", "size", "download_url",
           "page_url"} <= set(sc_it) and "gokapi_id" not in sc_it
          and "gokapiId" not in sc_it, str(sc_it)[:110])
    # execute surface (web API)
    b = (f'--B\r\nContent-Disposition: form-data; name="tool"\r\n\r\n'
         f'compress\r\n'
         f'--B\r\nContent-Disposition: form-data; name="client_ref"\r\n\r\n'
         f'f2-up-{_u}\r\n').encode()
    b += (f'--B\r\nContent-Disposition: form-data; name="file"; '
          f'filename="f2.png"\r\nContent-Type: image/png\r\n\r\n').encode() + PNG + b"\r\n"
    b += b"--B--\r\n"
    st, _h, d_up, _sc = _req("POST", "/api/uploads", b,
                             {"Content-Type": "multipart/form-data; boundary=B",
                              "Cookie": CK})
    _st = (d_up or {}).get("file") or {}
    st, _h, d_x, _sc = _req("POST", "/api/tools/execute", {
        "tool_id": "compress",
        "source": {"files": [{"public_id": _st.get("stagingId"),
                              "source_token": _st.get("sourceToken") or ""}]},
        "params": {}, "delivery": {"expiry_days": 1,
                                   "allowed_downloads": 50},
        "client_ref": f"f2-{_u}"},
        {"Content-Type": "application/json", "Cookie": CK})
    rec = ((d_x or {}).get("result") or {})
    rit = next((i for i in rec.get("items", []) if i.get("kind") == "file"), {})
    check("F2 web execute record item shares the same snake_case shape",
          {"kind", "filename", "content_type", "size", "public_id",
           "page_url", "download_url"} <= set(rit)
          and "gokapiId" not in rit and "gokapi_id" not in rit, str(rit)[:110])
    check("F3 record head is snake_case (result_id/result_url/expires_at)",
          rec.get("result_id") and rec.get("result_url", "").endswith(
              rec["result_id"]) and isinstance(rec.get("expires_at"), int),
          str({k: rec.get(k) for k in ("result_id", "result_url", "expires_at")}))
    # /r page renders the same record
    rid = rec.get("result_id")
    st, _h, rbody, _sc = _req("GET", f"/r/{rid}")
    check("F4 /r page renders from the unified record",
          st == 200 and 'class="rs-item' in rbody, st)
    check("F5 no storage id anywhere in the /r HTML",
          "gokapi" not in rbody.lower(), "gokapi leaked")

finally:
    with db() as con:
        con.execute("delete from operator_audit where actor_user_id = ANY(%s)", (created,))
        for t in created:
            purge_users(con, [t])
        con.execute("delete from platform_tools where tool_key like %s", (f"%{_u}%",))
        con.execute("delete from mcp_upload_tickets where client_ref like %s", (f"%{_u}%",))
        con.execute("delete from tool_calls where client_ref like %s", (f"%{_u}%",))
        con.execute("delete from memberships where provider_subscription_id like %s", (f"%{_u}%",))
    print("cleanup done")


# ════════════════════════════════════════════════════════════════════════
# Payment reverse
#
# Payment reverse canonical — refund/dispute → exact economic reversal.
#
# Proves over real functions/routes (Creem isolated by module fakes, DB
# isolated):
#   package: +10000 → spend 7000 → full refund → -7000 (negative legal,
#            exact original grant, snapshot-checked, catalog edits
#            irrelevant);
#   subscription: reversal reads ONLY transaction-linked grant rows
#            (never current Plan monthly_credits); current-period refund/
#            dispute expires Membership NOW; historical reversal leaves
#            the current period untouched;
#   idempotency: payment-reversal:{txn} survives refund replay, dispute
#            replay, refund-then-dispute, and concurrent double fire;
#   fail-closed: partial refund / unknown txn / provider fetch failure →
#            zero mutation + 500; subscription.update alone grants zero.
# ════════════════════════════════════════════════════════════════════════
section('Payment reverse')

import os as _os
import sys as _sys


import time as _time
ts_rev = int(_time.time())

from database import db

def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts_rev, ts_rev)).fetchone()
    return int(row["id"])

_u_rev = "rv" + _os.urandom(4).hex()

from economy.billing import spendable_balance, grant, consume, revoke
from repo import orders as orders_repo

# ═══ fake provider transaction store (fetch_transaction is patched) ═══
from economy import reconcile as _rec
from economy import creem as _creem

_TXNS = {}
def _fake_fetch_transaction(txn_id):
    _FETCHED.append(txn_id)
    t = _TXNS.get(txn_id)
    return dict(t) if t else None
_rec.fetch_transaction = _fake_fetch_transaction
_FETCH_FAIL = {"fail": False}
_orig_fetch_transaction = _rec.fetch_transaction
def _fetch_maybe_failing(txn_id):
    if _FETCH_FAIL["fail"]:
        return None
    return _orig_fetch_transaction(txn_id)
_rec.fetch_transaction = _fetch_maybe_failing

# webhook signature: set a test secret + compute HMAC like Creem
import hmac as _hmac
import hashlib as _hashlib
import json as _json
_creem.CREEM_WEBHOOK_SECRET = "rv-test-secret"
def _signed(event):
    raw = _json.dumps(event).encode()
    sig = _hmac.new(b"rv-test-secret", raw, _hashlib.sha256).hexdigest()
    return raw, sig

from fastapi.testclient import TestClient
from app.main import app as _app
client = TestClient(_app)

_FETCHED = []   # txn ids the provider fetch actually received

def _fire(event_type, txn_id, *, obj_id=None, obj_type=None,
          txn_nested=True, txn_id_override=None):
    """Fire a signed webhook with the REAL Creem shape:
    event.id=evt_*; object.id=ref_*/disp_* (NOT the transaction id);
    object.object=refund|dispute; object.transaction.id=tran_*.
    Returns (status, json)."""
    if obj_id is None:
        obj_id = ("ref_" if event_type == "refund.created" else "disp_") + txn_id
    obj = {"id": obj_id,
           "object": obj_type or ("refund" if event_type == "refund.created"
                                  else "dispute")}
    if txn_nested:
        obj["transaction"] = {"id": txn_id_override or txn_id}
    event = {"id": f"evt_{txn_id}", "eventType": event_type, "object": obj}
    raw, sig = _signed(event)
    r = client.post("/webhooks/creem", content=raw,
                    headers={"creem-signature": sig,
                             "Content-Type": "application/json"})
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}

def _iso(v):
    import datetime
    return datetime.datetime.fromtimestamp(v, datetime.timezone.utc).isoformat()

# ══════════ A. PACKAGE reversal ══════════
buyer = _mkuser(_u_rev + "b1")
# a credits package
with db() as con:
    _rv_pkg_sort = int(con.execute(
        "select coalesce(max(sort_order), 0) + 1 s from packages"
    ).fetchone()["s"])
    pkg = con.execute(
        "insert into packages (code, name, credits, price_cents, currency,"
        " active, sort_order, created_at, updated_at) values (%s, 'RV Pkg',"
        " 10000, 999, 'USD', TRUE, %s, %s, %s) returning id",
        (f"pkg_{_u_rev}", _rv_pkg_sort, ts_rev, ts_rev)).fetchone()
    pkg_id = int(pkg["id"])

# local order (as checkout would create) with frozen snapshot 10000
with db() as con:
    o = con.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, credits_snapshot, created_at)"
        " values (%s, 'package', %s, 'creem', %s, 'pending', 999, 'USD', 10000, %s)"
        " returning *", (buyer, pkg_id, f"ch:rv_{_u_rev}", ts_rev)).fetchone()
    order_id = int(o["id"])

# fulfill it via the canonical fulfillment (real Order.id, spec#2)
_creem.CREEM_CREDITS_PRODUCT_ID = "prod_credits_rv"
_creem.CREEM_CREDITS_CURRENCY = "USD"
res = _creem.fulfill_by_request_id(
    order_id, provider_order_id=f"ord_{_u_rev}", checkout_id=f"rv_{_u_rev}",
    amount_cents=999, product_id="prod_credits_rv", currency="USD")
check("A0 fulfill grants 10000 (snapshot) + provider id 收敛 ord_xxx",
      res.get("granted") and int(res.get("credits")) == 10000,
      str(res))
with db() as con:
    row = con.execute("select provider_order_id, status from orders where id=%s",
                      (order_id,)).fetchone()
check("A0a local order now bound to canonical ord_xxx",
      row["provider_order_id"] == f"ord_{_u_rev}" and row["status"] == "paid",
      str(dict(row)))
check("A0b spendable = 10000", spendable_balance(buyer) == 10000,
      str(spendable_balance(buyer)))

# user spends 7000
consume(buyer, 7000, "consume", ref_type="test", ref_id="spend1",
        idem_key=f"spend:{_u_rev}:1", note="rv test spend")
check("A1 spent 7000 → balance 3000", spendable_balance(buyer) == 3000,
      str(spendable_balance(buyer)))

# operator edits the package to 5000 — reversal must NOT care
with db() as con:
    con.execute("update packages set credits=5000 where id=%s", (pkg_id,))

# full refund
_TXNS[f"txn_{_u_rev}r1"] = {
    "object": "transaction", "id": f"txn_{_u_rev}r1", "status": "refunded",
    "type": "payment", "order": f"ord_{_u_rev}",
}
sc, body = _fire("refund.created", f"txn_{_u_rev}r1")
check("A2 full refund → 200 + exact reversal fact",
      sc == 200 and body.get("reversed") is True and body.get("credits") == -10000,
      f"{sc} {body}")
check("A3 balance -7000 (debt legal, no insufficient check)",
      spendable_balance(buyer) == -7000, str(spendable_balance(buyer)))
with db() as con:
    orow = con.execute("select status, refunded_at from orders where id=%s",
                       (order_id,)).fetchone()
check("A4 order terminal state = refunded", orow["status"] == "refunded"
      and orow["refunded_at"] is not None, str(dict(orow)))

# replay same refund → no double revoke
sc, body = _fire("refund.created", f"txn_{_u_rev}r1")
check("A5 refund replay → idempotent no double revoke (balance still -7000)",
      sc == 200 and spendable_balance(buyer) == -7000,
      f"{sc} bal={spendable_balance(buyer)}")

# same transaction then disputed → still no second reversal
_TXNS[f"txn_{_u_rev}r1"] = {**_TXNS[f"txn_{_u_rev}r1"], "status": "chargeback"}
sc, body = _fire("dispute.created", f"txn_{_u_rev}r1")
check("A6 refund→dispute same txn → single reversal only",
      sc == 200 and spendable_balance(buyer) == -7000,
      f"{sc} bal={spendable_balance(buyer)}")

# ══════════ B. dispute full chargeback exact reversal ══════════
buyer2 = _mkuser(_u_rev + "b2")
with db() as con:
    o2 = con.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, credits_snapshot, created_at)"
        " values (%s, 'package', %s, 'creem', %s, 'pending', 999, 'USD', 10000, %s)"
        " returning *", (buyer2, pkg_id, f"ch:rv2_{_u_rev}", ts_rev)).fetchone()
    order2_id = int(o2["id"])
_creem.fulfill_by_request_id(
    order2_id, provider_order_id=f"ord2_{_u_rev}", checkout_id=f"rv2_{_u_rev}",
    amount_cents=999, product_id="prod_credits_rv", currency="USD")
_TXNS[f"txn_{_u_rev}d1"] = {
    "object": "transaction", "id": f"txn_{_u_rev}d1", "status": "chargeback",
    "type": "payment", "order": f"ord2_{_u_rev}",
}
b0 = spendable_balance(buyer2)
sc, body = _fire("dispute.created", f"txn_{_u_rev}d1")
check("B1 dispute full chargeback → exact -10000",
      sc == 200 and spendable_balance(buyer2) == b0 - 10000,
      f"{sc} bal={spendable_balance(buyer2)}")
with db() as con:
    orow = con.execute("select status from orders where id=%s",
                       (order2_id,)).fetchone()
check("B2 order terminal = disputed", orow["status"] == "disputed",
      str(dict(orow)))

# ══════════ C. partial refund → zero mutation + 500 ══════════
buyer3 = _mkuser(_u_rev + "b3")
with db() as con:
    o3 = con.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, credits_snapshot, created_at)"
        " values (%s, 'package', %s, 'creem', %s, 'pending', 999, 'USD', 10000, %s)"
        " returning *", (buyer3, pkg_id, f"ch:rv3_{_u_rev}", ts_rev)).fetchone()
    order3_id = int(o3["id"])
_creem.fulfill_by_request_id(
    order3_id, provider_order_id=f"ord3_{_u_rev}", checkout_id=f"rv3_{_u_rev}",
    amount_cents=999, product_id="prod_credits_rv", currency="USD")
b3_0 = spendable_balance(buyer3)
_TXNS[f"txn_{_u_rev}p1"] = {
    "object": "transaction", "id": f"txn_{_u_rev}p1", "status": "partially_refunded",
    "type": "payment", "order": f"ord3_{_u_rev}",
}
sc, body = _fire("refund.created", f"txn_{_u_rev}p1")
check("C1 partial refund → 200 review queue + zero mutation ",
      sc == 200 and body.get("kind") == "review"
      and body.get("reason") == "partial_refund"
      and spendable_balance(buyer3) == b3_0,
      f"{sc} {body} bal={spendable_balance(buyer3)}")
with db() as con:
    rv_row = con.execute(
        "select status, event_id from payment_reviews where txn_id=%s"
        " order by id desc limit 1", (f"txn_{_u_rev}p1",)).fetchone()
check("C1b partial refund queued open for operator review",
      rv_row is not None and rv_row["status"] == "open"
      and rv_row["event_id"] == f"evt_txn_{_u_rev}p1", str(dict(rv_row or {})))
sc, body = _fire("refund.created", f"txn_{_u_rev}p1")
with db() as con:
    n_rv = con.execute(
        "select count(*) c from payment_reviews where txn_id=%s",
        (f"txn_{_u_rev}p1",)).fetchone()["c"]
check("C1c partial-refund replay → still 200, still ONE review row",
      sc == 200 and n_rv == 1, f"{sc} n={n_rv}")
with db() as con:
    orow = con.execute("select status from orders where id=%s",
                       (order3_id,)).fetchone()
check("C2 order untouched (still paid)", orow["status"] == "paid",
      str(dict(orow)))

# ══════════ D. unknown txn / fetch failure → 500 + zero mutation ══════════
sc, body = _fire("refund.created", "txn_unknown_rv")
check("D1 unknown transaction → 500 zero mutation",
      sc == 500 and spendable_balance(buyer3) == b3_0,
      f"{sc} {body}")
_FETCH_FAIL["fail"] = True
_TXNS[f"txn_{_u_rev}f1"] = {
    "object": "transaction", "id": f"txn_{_u_rev}f1", "status": "refunded",
    "type": "payment", "order": f"ord3_{_u_rev}",
}
sc, body = _fire("refund.created", f"txn_{_u_rev}f1")
check("D2 provider fetch failure → 500 zero mutation",
      sc == 500 and spendable_balance(buyer3) == b3_0,
      f"{sc} {body}")
_FETCH_FAIL["fail"] = False

# grant/snapshot mismatch → fail loud (construct synthetic order)
buyer4 = _mkuser(_u_rev + "b4")
with db() as con:
    o4 = con.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, credits_snapshot, created_at)"
        " values (%s, 'package', %s, 'creem', %s, 'pending', 999, 'USD', 10000, %s)"
        " returning *", (buyer4, pkg_id, f"ch:rv4_{_u_rev}", ts_rev)).fetchone()
    order4_id = int(o4["id"])
_creem.fulfill_by_request_id(
    order4_id, provider_order_id=f"ord4_{_u_rev}", checkout_id=f"rv4_{_u_rev}",
    amount_cents=999, product_id="prod_credits_rv", currency="USD")
# doctor the grant ledger delta to disagree with the snapshot
with db() as con:
    con.execute(
        "update ledger set delta = 5000 where user_id=%s and idem_key=%s",
        (buyer4, f"order:{order4_id}"))
_TXNS[f"txn_{_u_rev}m1"] = {
    "object": "transaction", "id": f"txn_{_u_rev}m1", "status": "refunded",
    "type": "payment", "order": f"ord4_{_u_rev}",
}
sc, body = _fire("refund.created", f"txn_{_u_rev}m1")
check("D3 grant/snapshot mismatch → 500 fail loud, no guess",
      sc == 500 and "mismatch" in str(body.get("error")),
      f"{sc} {body}")

# ══════════ S. SUBSCRIPTION reversal ══════════
from economy.subscription import handle_event as _hev

def _sub_snap(status, ps, pe, product, sub_id, txn=None):
    s = {"object": "subscription", "id": sub_id, "status": status,
         "current_period_start_date": _iso(ps),
         "current_period_end_date": _iso(pe),
         "product": {"id": product}}
    if txn:
        s["last_transaction_id"] = txn
    return s

# two paid plans (plus/pro) with distinct products
from app.web_auth import create_browser_session, COOKIE_NAME
op = _mkuser(_u_rev + "op")
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (op,))
op_sess = create_browser_session(op)
def _mkplan(code, prod, price, monthly, so=None):
    if so is None:
        with db() as _c:
            so = int(_c.execute(
                "select coalesce(max(sort_order), 0) + 1 s from plans"
            ).fetchone()["s"])
    _creem.CREEM_API_KEY = "test-key"
    r = client.post("/samelabs/commerce/products/plans", json={
        "code": f"{code}_{_u_rev}", "name": code.upper(), "monthly_credits": monthly,
        "price_cents": price, "creem_product_id": prod, "sort_order": so,
        "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
        "expiry_days": [7], "download_limits": [50],
    }, cookies={COOKIE_NAME: op_sess})
    assert r.status_code == 200, r.text
    return int(r.json()["plan"]["id"])
# mirror state for plan creation write-through
_ms = getattr(__builtins__, "_MIRROR", None)
import economy.creem as _cm
# reuse forward-style mirror fakes minimally: patch transport
_MIRROR = {"prod_rv_plus": {"price": 999, "currency": "USD",
                            "billing_type": "recurring",
                            "billing_period": "every-month"},
           "prod_rv_pro": {"price": 2999, "currency": "USD",
                           "billing_type": "recurring",
                           "billing_period": "every-month"}}
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
_cm._fetch_product = _fp
_cm._patch = _pt
_cm.CREEM_API_KEY = "test-key"

plus_id = _mkplan("rvplus", "prod_rv_plus", 999, 100)
pro_id = _mkplan("rvpro", "prod_rv_pro", 2999, 500)

PS = ts_rev - 5 * 86400
PE = ts_rev + 25 * 86400

def _seed_sub_user(name, plan_id, product, sub_id):
    uid = _mkuser(name)
    with db() as con:
        con.execute(
            "insert into orders (user_id, kind, item_id, provider,"
            " provider_order_id, status, amount_cents, currency, paid_at,"
            " created_at) values (%s, 'plan', %s, 'creem', %s, 'paid', 999,"
            " 'USD', %s, %s)",
            (uid, plan_id, "sub:" + sub_id, ts_rev, ts_rev))
        con.execute(
            "insert into memberships (user_id, plan_id, provider_subscription_id,"
            " status, started_at, renews_at, last_grant_period_start, updated_at)"
            " values (%s, %s, %s, 'active', %s, %s, %s, %s)",
            (uid, plan_id, "sub:" + sub_id, PS, PE, PS, ts_rev))
    return uid

# S1/S2 sequence: base paid FIRST, then update sync, then upgrade paid.
u1 = _seed_sub_user(_u_rev + "s1", plus_id, "prod_rv_plus", f"sub_{_u_rev}s1")
b1 = spendable_balance(u1)
# S1: update alone on a FRESH member (no prior paid) must grant nothing
_hev("subscription.update", _sub_snap("active", PS, PE, "prod_rv_plus",
                                      f"sub_{_u_rev}s1"))
check("S1 subscription.update 单独到达 → zero Credits (paid-proof only)",
      spendable_balance(u1) == b1,
      f"bal={spendable_balance(u1)} pre={b1}")
# S2a: base paid (plus) → +100
_hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                    f"sub_{_u_rev}s1", txn=f"txn_{_u_rev}s1a"))
b1_after_base = spendable_balance(u1)
check("S2a base paid grant = 100 (plus)",
      b1_after_base - b1 == 100, f"{b1}→{b1_after_base}")
with db() as con:
    g = con.execute(
        "select kind, ref_type, ref_id from ledger where user_id=%s"
        " and kind in ('grant_plan','grant_plan_upgrade')"
        " and ref_type='creem_transaction'", (u1,)).fetchall()
check("S2b base grant ref = creem_transaction / txn id",
      len(g) == 1 and g[0]["kind"] == "grant_plan"
      and g[0]["ref_id"] == f"txn_{_u_rev}s1a", str([dict(x) for x in g]))
# S2c: upgrade — update syncs Membership to pro (ZERO credits), the
# upgrade paid transaction produces the +400 top-up in apply_paid_period
b_pre_sync = spendable_balance(u1)
_hev("subscription.update", _sub_snap("active", PS, PE, "prod_rv_pro",
                                      f"sub_{_u_rev}s1"))
check("S2c0 update sync alone still grants zero (already base-paid)",
      spendable_balance(u1) == b_pre_sync,
      f"{b_pre_sync}→{spendable_balance(u1)}")
_hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_pro",
                                    f"sub_{_u_rev}s1", txn=f"txn_{_u_rev}s1b"))
check("S2c paid upgrade → apply_paid_period 补差 +400 (100→500)",
      spendable_balance(u1) - b1_after_base == 400,
      f"{b1_after_base}→{spendable_balance(u1)}")
with db() as con:
    g2 = con.execute(
        "select kind, ref_id from ledger where user_id=%s"
        " and kind='grant_plan_upgrade'", (u1,)).fetchall()
check("S2d upgrade grant ref = txn id",
      len(g2) == 1 and g2[0]["ref_id"] == f"txn_{_u_rev}s1b",
      str([dict(x) for x in g2]))

# S3: full refund of BASE txn → exact base reversal (-100)
_TXNS[f"txn_{_u_rev}s1a"] = {"object": "transaction", "id": f"txn_{_u_rev}s1a",
                         "status": "refunded", "type": "invoice",
                         "subscription": f"sub_{_u_rev}s1",
                         "period_start": _iso(PS), "period_end": _iso(PE)}
b_pre = spendable_balance(u1)
sc, body = _fire("refund.created", f"txn_{_u_rev}s1a")
check("S3 base refund → exact -100 reversal",
      sc == 200 and spendable_balance(u1) == b_pre - 100,
      f"{sc} {body} bal={spendable_balance(u1)}")

# S3b: spend 300 → refunds will pull below zero (real debt)
consume(u1, 300, "consume", ref_type="test", ref_id="spends1",
        idem_key=f"spend:{_u_rev}:s1", note="rv sub spend")

# S4: full refund of UPGRADE txn → exact -400
_TXNS[f"txn_{_u_rev}s1b"] = {"object": "transaction", "id": f"txn_{_u_rev}s1b",
                         "status": "refunded", "type": "invoice",
                         "subscription": f"sub_{_u_rev}s1",
                         "period_start": _iso(PS), "period_end": _iso(PE)}
b_pre = spendable_balance(u1)
sc, body = _fire("refund.created", f"txn_{_u_rev}s1b")
check("S4 upgrade refund → exact -400 reversal",
      sc == 200 and spendable_balance(u1) == b_pre - 400,
      f"{sc} {body} bal={spendable_balance(u1)}")
check("S4b reversal pulled spendable negative (debt): 500-300-100-400 = -300",
      spendable_balance(u1) == -300, str(spendable_balance(u1)))

# S5: current-period refund → Membership immediately expired
u5 = _seed_sub_user(_u_rev + "s5", plus_id, "prod_rv_plus", f"sub_{_u_rev}s5")
_hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                    f"sub_{_u_rev}s5", txn=f"txn_{_u_rev}s5"))
_TXNS[f"txn_{_u_rev}s5"] = {"object": "transaction", "id": f"txn_{_u_rev}s5",
                        "status": "refunded", "type": "invoice",
                        "subscription": f"sub_{_u_rev}s5",
                        "period_start": _iso(PS), "period_end": _iso(PE)}
sc, body = _fire("refund.created", f"txn_{_u_rev}s5")
with db() as con:
    m5 = con.execute(
        "select status, renews_at from memberships where user_id=%s",
        (u5,)).fetchone()
import time as _t2
_now5 = int(_t2.time())
check("S5 current-period refund → Membership expired immediately",
      sc == 200 and m5["status"] == "expired"
      and int(m5["renews_at"]) <= _now5 + 2,
      f"{sc} {dict(m5)} now={_now5}")

# S6: current-period dispute → immediately expired
u6 = _seed_sub_user(_u_rev + "s6", plus_id, "prod_rv_plus", f"sub_{_u_rev}s6")
_hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                    f"sub_{_u_rev}s6", txn=f"txn_{_u_rev}s6"))
_TXNS[f"txn_{_u_rev}s6"] = {"object": "transaction", "id": f"txn_{_u_rev}s6",
                        "status": "chargeback", "type": "invoice",
                        "subscription": f"sub_{_u_rev}s6",
                        "period_start": _iso(PS), "period_end": _iso(PE)}
sc, body = _fire("dispute.created", f"txn_{_u_rev}s6")
with db() as con:
    m6 = con.execute(
        "select status from memberships where user_id=%s", (u6,)).fetchone()
check("S6 current-period dispute → Membership expired",
      sc == 200 and m6["status"] == "expired", f"{sc} {dict(m6)}")

# S5r: the refund's period check re-reads the membership row FOR UPDATE —
# a concurrent transaction holding the row lock and committing a NEW
# period must be seen by the refund (historical period → membership
# untouched), never the stale pre-commit snapshot.
u5r = _seed_sub_user(_u_rev + "s5r", plus_id, "prod_rv_plus", f"sub_{_u_rev}s5r")
_hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                    f"sub_{_u_rev}s5r", txn=f"txn_{_u_rev}s5r"))
_TXNS[f"txn_{_u_rev}s5r"] = {"object": "transaction", "id": f"txn_{_u_rev}s5r",
                         "status": "refunded", "type": "invoice",
                         "subscription": f"sub_{_u_rev}s5r",
                         "period_start": _iso(PS), "period_end": _iso(PE)}
PS5r, PE5r = PE, PE + 30 * 86400
from database import _database_dsn_kwargs as _dsn_kwargs
import psycopg2 as _psycopg
import threading as _threading
import time as _t5r
_conn_a = _psycopg.connect(**_dsn_kwargs())
_conn_a.autocommit = False
_cur_a = _conn_a.cursor()
# 事务 A: 持会员行锁并推进新周期 (未提交 —— 其他事务快照仍是旧周期)
_cur_a.execute(
    "update memberships set last_grant_period_start=%s, renews_at=%s,"
    " status='active', updated_at=%s where provider_subscription_id=%s",
    (PS5r, PE5r, ts_rev, "sub:" + f"sub_{_u_rev}s5r"))
_bal5r = spendable_balance(u5r)
_r5res = {}
def _r5fire():
    _r5res["fire"] = _fire("refund.created", f"txn_{_u_rev}s5r")
_th5r = _threading.Thread(target=_r5fire)
_th5r.start()
# 等 FOR UPDATE 真正阻塞在 memberships 行上 (锁定读先于 A 提交, 窗口确定)
_blocked5r = False
for _ in range(100):
    with db() as con:
        _w = con.execute(
            "select 1 from pg_stat_activity where wait_event_type='Lock'"
            " and position('for update' in lower(query)) > 0"
            " and pid <> pg_backend_pid()"
        ).fetchone()
    if _w:
        _blocked5r = True
        break
    _t5r.sleep(0.05)
_conn_a.commit()
_th5r.join(30)
_conn_a.close()
with db() as con:
    m5r = con.execute(
        "select status, renews_at, last_grant_period_start from memberships"
        " where user_id=%s", (u5r,)).fetchone()
check("S5r lock-race: refund blocked on the row lock, sees the committed new"
      " period (historical → membership untouched)",
      _blocked5r and _r5res["fire"][0] == 200 and m5r["status"] == "active"
      and int(m5r["last_grant_period_start"]) == PS5r
      and int(m5r["renews_at"]) == PE5r
      and spendable_balance(u5r) == _bal5r - 100,
      f"blocked={_blocked5r} {_r5res['fire']} {dict(m5r)}"
      f" bal={spendable_balance(u5r)}")

# S7: HISTORICAL period refund → current paid Membership untouched
u7 = _seed_sub_user(_u_rev + "s7", plus_id, "prod_rv_plus", f"sub_{_u_rev}s7")
OLD_PS, OLD_PE = ts_rev - 40 * 86400, ts_rev - 10 * 86400   # january
CUR_PS, CUR_PE = ts_rev - 5 * 86400, ts_rev + 25 * 86400    # february paid & current
_hev("subscription.paid", _sub_snap("active", OLD_PS, OLD_PE, "prod_rv_plus",
                                    f"sub_{_u_rev}s7", txn=f"txn_{_u_rev}s7old"))
_hev("subscription.paid", _sub_snap("active", CUR_PS, CUR_PE, "prod_rv_plus",
                                    f"sub_{_u_rev}s7", txn=f"txn_{_u_rev}s7cur"))
with db() as con:
    con.execute(
        "update memberships set last_grant_period_start=%s where user_id=%s",
        (CUR_PS, u7))
_TXNS[f"txn_{_u_rev}s7old"] = {"object": "transaction", "id": f"txn_{_u_rev}s7old",
                           "status": "refunded", "type": "invoice",
                           "subscription": f"sub_{_u_rev}s7",
                           "period_start": _iso(OLD_PS), "period_end": _iso(OLD_PE)}
b_pre = spendable_balance(u7)
sc, body = _fire("refund.created", f"txn_{_u_rev}s7old")
with db() as con:
    m7 = con.execute(
        "select status, renews_at, last_grant_period_start from memberships"
        " where user_id=%s", (u7,)).fetchone()
check("S7 historical refund → exact -100 but current Membership intact",
      sc == 200 and spendable_balance(u7) == b_pre - 100
      and m7["status"] == "active" and int(m7["renews_at"]) == CUR_PE
      and int(m7["last_grant_period_start"]) == CUR_PS,
      f"{sc} {body} {dict(m7)}")

# S8: wrong subscription / wrong type / unknown order → zero mutation + 500
u8 = _seed_sub_user(_u_rev + "s8", plus_id, "prod_rv_plus", f"sub_{_u_rev}s8")
_hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                    f"sub_{_u_rev}s8", txn=f"txn_{_u_rev}s8"))
b8 = spendable_balance(u8)
_TXNS[f"txn_{_u_rev}s8w"] = {"object": "transaction", "id": f"txn_{_u_rev}s8w",
                         "status": "refunded", "type": "invoice",
                         "subscription": "sub_somebody_else",
                         "period_start": _iso(PS), "period_end": _iso(PE)}
sc, body = _fire("refund.created", f"txn_{_u_rev}s8w")
check("S8a unknown subscription order → 500 zero mutation",
      sc == 500 and spendable_balance(u8) == b8, f"{sc} {body}")
_TXNS[f"txn_{_u_rev}s8t"] = {"object": "transaction", "id": f"txn_{_u_rev}s8t",
                         "status": "refunded", "type": "subscription",
                         "subscription": f"sub_{_u_rev}s8"}
sc, body = _fire("refund.created", f"txn_{_u_rev}s8t")
check("S8b wrong type → 500 zero mutation",
      sc == 500 and spendable_balance(u8) == b8, f"{sc} {body}")

# S9: period aggregation nets reversal (spec §9)
from repo import orders as _ord
with db() as con:
    net_cur = _ord.period_plan_credits(con, u7, CUR_PS)
    net_old = _ord.period_plan_credits(con, u7, OLD_PS)
check("S9 period aggregation: current period intact (100), reversed period = 0",
      net_cur == 100 and net_old == 0, f"cur={net_cur} old={net_old}")

# S10: concurrent double-fire same refund → single reversal
u10 = _seed_sub_user(_u_rev + "s10", plus_id, "prod_rv_plus", f"sub_{_u_rev}s10")
_hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                    f"sub_{_u_rev}s10", txn=f"txn_{_u_rev}s10"))
_TXNS[f"txn_{_u_rev}s10"] = {"object": "transaction", "id": f"txn_{_u_rev}s10",
                         "status": "refunded", "type": "invoice",
                         "subscription": f"sub_{_u_rev}s10",
                         "period_start": _iso(PS), "period_end": _iso(PE)}
b10 = spendable_balance(u10)
import threading
_res = {}
def _fire10():
    _res["r"] = _fire("refund.created", f"txn_{_u_rev}s10")
t = threading.Thread(target=_fire10)
t.start()
sc2, _ = _fire("refund.created", f"txn_{_u_rev}s10")
t.join(timeout=30)
rev_rows = None
with db() as con:
    rev_rows = con.execute(
        "select count(*) c from ledger where user_id=%s and kind='revoke'"
        " and idem_key=%s", (u10, f"payment-reversal:txn_{_u_rev}s10")).fetchone()
check("S10 concurrent double fire → exactly one revoke row (-100 once)",
      spendable_balance(u10) == b10 - 100 and int(rev_rows["c"]) == 1,
      f"bal={spendable_balance(u10)} rows={dict(rev_rows)}")

# ══════════ R. identity shape / reversed-period gate / recovery ══════════

# R1/R2: official shapes — ref_x != tran_x, disp_x != tran_x; fetch MUST
# receive the NESTED transaction id, never the refund/dispute object id.
_FETCHED.clear()
sc, body = _fire("refund.created", f"tran_{_u_rev}x1",
                 obj_id=f"ref_{_u_rev}x1")
check("R1 official refund shape: fetch receives tran_x (not ref_x)",
      f"tran_{_u_rev}x1" in _FETCHED and f"ref_{_u_rev}x1" not in _FETCHED,
      str(_FETCHED[-3:]))
_FETCHED.clear()
_TXNS[f"tran_{_u_rev}x2"] = {"object": "transaction", "id": f"tran_{_u_rev}x2",
                         "status": "chargeback", "type": "payment",
                         "order": f"ord2_{_u_rev}"}
sc, body = _fire("dispute.created", f"tran_{_u_rev}x2", obj_id=f"disp_{_u_rev}x2")
check("R2 official dispute shape: fetch receives tran_x (not disp_x)",
      _FETCHED and _FETCHED[-1] == f"tran_{_u_rev}x2", str(_FETCHED[-2:]))

# R3: missing nested transaction → 500 / zero mutation
b_r3 = spendable_balance(buyer3)
sc, body = _fire("refund.created", "txn_never_fetched", txn_nested=False)
check("R3 missing nested transaction → 500 reversal_proof_missing + zero mutation",
      sc == 500 and body.get("error") == "reversal_proof_missing"
      and spendable_balance(buyer3) == b_r3, f"{sc} {body}")

# R4: wrong object type → 500
sc, body = _fire("refund.created", f"txn_{_u_rev}s8",
                 obj_type="dispute")
check("R4 wrong object.object type → 500 object_type_mismatch",
      sc == 500 and "object_type_mismatch" in str(body.get("error")),
      f"{sc} {body}")

# R5-R8: reversed-period replay gate + new-period recovery
u_r = _seed_sub_user(_u_rev + "rr", plus_id, "prod_rv_plus", f"sub_{_u_rev}rr")
_hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                    f"sub_{_u_rev}rr", txn=f"tran_{_u_rev}rr1"))
b_base = spendable_balance(u_r)
_TXNS[f"tran_{_u_rev}rr1"] = {"object": "transaction", "id": f"tran_{_u_rev}rr1",
                          "status": "refunded", "type": "invoice",
                          "subscription": f"sub_{_u_rev}rr",
                          "period_start": _iso(PS), "period_end": _iso(PE)}
sc, body = _fire("refund.created", f"tran_{_u_rev}rr1")
with db() as con:
    m_r = con.execute("select status, renews_at from memberships"
                      " where user_id=%s", (u_r,)).fetchone()
check("R5 current period refund → -100 + Membership expired",
      sc == 200 and spendable_balance(u_r) == b_base - 100
      and m_r["status"] == "expired",
      f"{sc} {body} {dict(m_r)}")

# R6: SAME period old paid replay → zero grant + remains expired
b_r6 = spendable_balance(u_r)
try:
    _hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                        f"sub_{_u_rev}rr", txn=f"tran_{_u_rev}rr1"))
    r6 = "no-raise"
except KeyError as e:
    r6 = str(e)
with db() as con:
    m_r6 = con.execute("select status from memberships where user_id=%s",
                       (u_r,)).fetchone()
check("R6 同 period 旧 paid replay → paid_period_reversed + zero grant + 仍 expired",
      "paid_period_reversed" in r6 and spendable_balance(u_r) == b_r6
      and m_r6["status"] == "expired",
      f"{r6} bal={spendable_balance(u_r)} {dict(m_r6)}")

# R7: SAME period ANOTHER old paid transaction replay → still gated
# (the period itself is reversed; a different old txn of the same
# period must not resurrect it either)
b_r7 = spendable_balance(u_r)
try:
    _hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                        f"sub_{_u_rev}rr", txn=f"tran_{_u_rev}rr_old2"))
    r7 = "no-raise"
except KeyError as e:
    r7 = str(e)
check("R7 同 period 另一旧 paid txn replay → 仍被 gate + zero grant",
      "paid_period_reversed" in r7 and spendable_balance(u_r) == b_r7,
      f"{r7} bal={spendable_balance(u_r)}")

# R8: NEW period, NEW matching paid transaction → normal recovery
PS2, PE2 = ts_rev + 86400, ts_rev + 31 * 86400
b_r8 = spendable_balance(u_r)
_hev("subscription.paid", _sub_snap("active", PS2, PE2, "prod_rv_plus",
                                    f"sub_{_u_rev}rr", txn=f"tran_{_u_rev}rr2"))
with db() as con:
    m_r8 = con.execute("select status, renews_at from memberships"
                       " where user_id=%s", (u_r,)).fetchone()
check("R8 新 period matching paid txn → 正常 +100 grant + Membership 重新 active",
      spendable_balance(u_r) == b_r8 + 100 and m_r8["status"] == "active"
      and int(m_r8["renews_at"]) == PE2,
      f"bal={spendable_balance(u_r)} {dict(m_r8)}")

# R9: missing / invalid period_end → zero reversal + 500
u_r9 = _seed_sub_user(_u_rev + "r9", plus_id, "prod_rv_plus", f"sub_{_u_rev}r9")
_hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                    f"sub_{_u_rev}r9", txn=f"tran_{_u_rev}r9"))
b_r9 = spendable_balance(u_r9)
_TXNS[f"tran_{_u_rev}r9"] = {"object": "transaction", "id": f"tran_{_u_rev}r9",
                         "status": "refunded", "type": "invoice",
                         "subscription": f"sub_{_u_rev}r9",
                         "period_start": _iso(PS), "period_end": None}
sc, body = _fire("refund.created", f"tran_{_u_rev}r9")
_TXNS[f"tran_{_u_rev}r9b"] = {"object": "transaction", "id": f"tran_{_u_rev}r9b",
                          "status": "refunded", "type": "invoice",
                          "subscription": f"sub_{_u_rev}r9",
                          "period_start": _iso(PE),
                          "period_end": _iso(PS)}   # end <= start
scb, bodyb = _fire("refund.created", f"tran_{_u_rev}r9b")
check("R9 缺失/乱序 period_end → 500 + zero mutation (两形态)",
      sc == 500 and "period_proof_invalid" in str(body.get("error"))
      and scb == 500 and "period_proof_invalid" in str(bodyb.get("error"))
      and spendable_balance(u_r9) == b_r9,
      f"{sc}/{body.get('error')} {scb}/{bodyb.get('error')}")

# R10: dispute for an unbound / never-paid local intent → fail loud
u_r10 = _mkuser(_u_rev + "r10")
# pending/failed rows may only carry NULL or ch:* identity. Therefore the
# legal unresolved shape is: 518 has a pending local intent with NO final
# provider identity, while the provider reversal references a canonical
# order identity that 518 never bound. This must fail loud with zero
# economic / Order mutation.
with db() as con:
    o10 = orders_repo.insert_order(
        con, u_r10, "package", pkg_id, "creem", 999, "USD", ts_rev,
        credits_snapshot=100)
    order10 = int(o10["id"])
_TXNS[f"tran_{_u_rev}r10"] = {"object": "transaction", "id": f"tran_{_u_rev}r10",
                          "status": "chargeback", "type": "payment",
                          "order": f"ord10_{_u_rev}"}
b_r10 = spendable_balance(u_r10)
sc, body = _fire("dispute.created", f"tran_{_u_rev}r10")
with db() as con:
    or10 = con.execute(
        "select status, provider_order_id from orders where id=%s",
        (order10,)).fetchone()
check("R10 dispute for unbound never-paid order → fail loud + zero mutation",
      sc == 500 and "package_order_not_found" in str(body.get("error"))
      and spendable_balance(u_r10) == b_r10
      and or10["status"] == "pending" and or10["provider_order_id"] is None,
      f"{sc} {body} {dict(or10)}")

# R11: subscription reversal replay → reversed=false / credits=0
sc, body = _fire("refund.created", f"tran_{_u_rev}rr1")
check("R11 replay response 事实: reversed=false / credits=0",
      sc == 200 and body.get("reversed") is False
      and int(body.get("credits") or 0) == 0, f"{sc} {body}")


# ════════════════════════════════════════════════════════════════════════
# Topup route contract
#
# Topup route-contract canonical: membership actions under /api/topup/*.
#
# HTTP boundary regression: real ASGI app, real routing table, no direct
# route-function calls. Guards:
#   - POST /api/topup/subscribe   reaches the subscription handler
#   - POST /api/topup/plan-upgrade   reaches the upgrade handler
#   - POST /api/topup/plan-downgrade reaches the downgrade handler
#   - POST /api/topup/checkout (credits top-up) does not regress
#   - old bare paths (/subscribe, /plan-upgrade, /plan-downgrade) are 404
#
# Creem is isolated by module-level fakes (same pattern as
# test_membership_governance / test_payment_tiers). "Reaches the handler"
# is proven by handler-specific outcomes, not just non-404: subscribe
# creates a pending plan order + fake checkout URL; upgrade requires an
# active paid membership (plan_change_required / no_membership proves the
# handler ran its own gate); downgrade likewise proves its gate path.
#
# fail-closed: requires FLASHSHARE_TEST_PORT + TEST_DB_URL (isolated).
# ════════════════════════════════════════════════════════════════════════
section('Topup route contract')

import os as _os, sys as _sys
import sys, time


import psycopg2
import uuid


_con = psycopg2.connect(PG); _con.autocommit = True
_cur = _con.cursor()
_ts = int(time.time())

from app.main import app  # real ASGI app with the real routing table
from app.web_auth import create_browser_session, COOKIE_NAME
import warnings as _w
with _w.catch_warnings():
    _w.simplefilter("ignore", DeprecationWarning)  # starlette prefers httpx2; httpx fallback warns
    from starlette.testclient import TestClient

_cur.execute("delete from packages where code='tc_pkg'")
_cur.execute(
    "select id from users where username like 'tc%'")
purge_users(_cur, [r[0] for r in _cur.fetchall()])
def _new_user(prefix):
    u = prefix + uuid.uuid4().hex[:6]
    _cur.execute(
        "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
        " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id",
        (u, f"{u}@t.co", _ts, _ts))
    return u, _cur.fetchone()[0]

u1, uid1 = _new_user("tc1")
u2, uid2 = _new_user("tc2")
u3, uid3 = _new_user("tc3")
u4, uid4 = _new_user("tc4")
sess1 = create_browser_session(uid1)
sess2 = create_browser_session(uid2)
sess3 = create_browser_session(uid3)
sess4 = create_browser_session(uid4)

# ── Creem isolation (module-level fakes; no network) ────────────────────
from economy import creem as _creem
_creem_calls = {"create_sub": 0, "create_credits": 0}
def _fake_sub_checkout(order_id, plan, user):
    _creem_calls["create_sub"] += 1
    return {"ok": True, "checkout_url": f"https://creem.test/c/{order_id}",
            "checkout_id": f"ch_tc_{order_id}"}
def _fake_credits_checkout(order_id, cents, user):
    _creem_calls["create_credits"] += 1
    return {"ok": True, "checkout_url": f"https://creem.test/t/{order_id}",
            "checkout_id": f"ch_tc_{order_id}"}
_orig = (_creem.create_subscription_checkout, _creem.create_credits_checkout,
         _creem.creem_enabled, _creem.credits_ready, _creem.CREEM_CREDITS_CURRENCY)
_creem.create_subscription_checkout = _fake_sub_checkout
_creem.create_credits_checkout = _fake_credits_checkout
_creem.creem_enabled = lambda: True
_creem.credits_ready = lambda: True
_creem.CREEM_CREDITS_CURRENCY = "USD"  # checkout handler compares pkg currency to this constant

# sellable plan fixture: bind creem_product_id on the seeded plus plan.
# Save the original value first — restore exactly what was there, never
# assume NULL (a future non-NULL binding must survive this test).
_cur.execute("select creem_product_id, price_cents from plans where code='plus'")
_plus_orig_pid, _plus_orig_price = _cur.fetchone()
# sellable 合同 = active ∧ price>0 ∧ product 绑定; fixture 使 plus 真可售
_cur.execute("update plans set creem_product_id='price_tc_plus', price_cents=500 where code='plus'")
from governance.plan_catalog import validate_config as _vc
_vc()  # fixture 直写行后刷新进程内目录缓存

client_top = TestClient(app)

_ps_seq = [0]
def _post(path, body, cookie):
    r = client_top.post(path, json=body, cookies={COOKIE_NAME: cookie},
                    follow_redirects=False)
    try:
        d = r.json()
    except Exception:
        d = {"_raw": r.text[:120]}
    return r.status_code, d

# ═══ 1. subscribe reaches the subscription handler ═══
st, d = _post("/api/topup/subscribe", {"plan_id": None}, sess1)  # None → int() fails → handler's own 422
check("TR1 /api/topup/subscribe 不再404 (进入handler)",
      st in (200, 409, 422) and d.get("error") != "not_found", f"st={st} d={d}")
# valid plan → handler creates pending plan order + fake checkout
_cur.execute("select id from plans where code='plus'")
plus_id = _cur.fetchone()[0]
st, d = _post("/api/topup/subscribe", {"plan_id": plus_id}, sess1)
_cur.execute("select count(*) from orders where user_id=%s and kind='plan'", (uid1,))
_n_plan_orders = _cur.fetchone()[0]
check("TR2 subscribe 真入订阅handler (plan order 建立 + checkout_url 返回)",
      st == 200 and d.get("checkout_url", "").startswith("https://creem.test/")
      and _n_plan_orders == 1 and _creem_calls["create_sub"] == 1, f"st={st} d={d} n={_n_plan_orders}")

# ═══ 2. plan-upgrade reaches the upgrade handler ═══
st, d = _post("/api/topup/plan-upgrade", {"plan": "pro"}, sess2)
check("TR3 /api/topup/plan-upgrade 不再404 (进入handler)",
      st in (200, 409, 422) and d.get("error") != "not_found", f"st={st} d={d}")
check("TR4 upgrade 真入handler (无会员 → no_active_membership 自有门)",
      st in (409, 422) and d.get("error") in ("no_active_membership", "no_membership",
                                              "plan_change_required", "not_on_paid_tier", "bad_plan"), f"st={st} d={d}")

# ═══ 3. plan-downgrade reaches the downgrade handler ═══
st, d = _post("/api/topup/plan-downgrade", {"plan": "plus"}, sess3)
check("TR5 /api/topup/plan-downgrade 不再404 (进入handler)",
      st in (200, 409, 422) and d.get("error") != "not_found", f"st={st} d={d}")
check("TR6 downgrade 真入handler (无会员 → no_active_membership 自有拒绝)",
      st in (409, 422) and d.get("error") in ("no_active_membership", "no_membership",
                                              "plan_change_required", "not_on_paid_tier", "bad_plan"), f"st={st} d={d}")

# ═══ 4. credits checkout (existing) does not regress ═══
_cur.execute(
    "insert into packages (code, name, credits, price_cents, currency, active, created_at, updated_at)"
    " values ('tc_pkg', 'TC Pkg', 100, 500, 'USD', TRUE, %s, %s)"
    " on conflict (code) do update set active=TRUE returning id", (_ts, _ts))
_pkg_id = _cur.fetchone()[0]
st, d = _post("/api/topup/checkout", {"package_id": _pkg_id}, sess4)
check("TR7 /api/topup/checkout 积分充值不回归 (200 + checkout_url)",
      st == 200 and d.get("checkout_url", "").startswith("https://creem.test/")
      and _creem_calls["create_credits"] == 1, f"st={st} d={d}")

# ═══ 5. old bare paths are gone ═══
# 全局 404 handler 对非 API 404 静默 302 回首页 (既有行为), 故未命中路由
# 表现 302 而非 404; /api/* 前缀例外直接 404。两者都证明"路由表无此路径"。
for old in ("/subscribe", "/plan-upgrade", "/plan-downgrade"):
    st, d = _post(old, {}, sess1)
    loc = client_top.post(old, json={}, cookies={COOKIE_NAME: sess1},
                      follow_redirects=False).headers.get("location", "")
    check(f"TR8 旧裸路径 {old} 已退役 (404/405/全局302)",
          st in (404, 405) or (st == 302 and loc == "/"), f"st={st} loc={loc}")

# ── restore + cleanup ───────────────────────────────────────────────────
(_creem.create_subscription_checkout, _creem.create_credits_checkout,
 _creem.creem_enabled, _creem.credits_ready, _creem.CREEM_CREDITS_CURRENCY) = _orig
_cur.execute("update plans set creem_product_id=%s, price_cents=%s where code='plus'",
             (_plus_orig_pid, _plus_orig_price))
_cur.execute("delete from packages where code='tc_pkg'")
_cur.execute(
    "select id from users where username like 'tc%'")
purge_users(_cur, [r[0] for r in _cur.fetchall()])


# ════════════════════════════════════════════════════════════════════════
# Payment forward
#
# Payment forward canonical — product facts → Creem proof → fulfillment.
#
# P-series proves over real functions/routes (Creem isolated by module
# fakes, DB isolated):
#   subscription price authority (518 authoritative → Creem mirrored →
#   checkout verified; mismatch fail closed; no recurring custom_price);
#   package credits snapshot (checkout freezes quantity; later package
#   edits never rewrite a checked-out order; replay no double grant);
#   paid-period transaction proof (webhook proof fields; reconcile needs
#   a matching paid invoice; negative transaction states never grant).
# ════════════════════════════════════════════════════════════════════════
section('Payment forward')

import os as _os
import sys as _sys


import time as _time
import json as _json
ts_fwd = int(_time.time())

from database import db

def _mkuser_fwd(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts_fwd, ts_fwd)).fetchone()
    return int(row["id"])

_u_fwd = "pc" + _os.urandom(4).hex()
buyer = None  # registered via HTTP below (needs a real password)

# ═══ Creem isolation (module fakes; no network) ═══
from economy import creem as _creem
from economy import reconcile as _rec_fwd

_mirror_state = {"prod_pc1": {"price": 999, "currency": "USD",
                              "billing_type": "recurring",
                              "billing_period": "every-month"}}
_patch_calls = {"n": 0, "fail": False, "last": None}
_txns = {}          # txn_id -> transaction payload

def _fake_mirror(product_id, price_cents, currency):
    # mirror the REAL write-through contract: GET-first (billing contract
    # must already be correct — never repaired), PATCH price/currency
    # ONLY, response re-confirmed.
    _patch_calls["n"] += 1
    if _patch_calls["fail"]:
        raise RuntimeError("provider down")
    prod = _mirror_state.get(product_id)
    if prod is None:
        raise RuntimeError("product_not_found")
    if prod["billing_type"] != "recurring" \
            or prod.get("billing_period") != "every-month":
        raise RuntimeError("product_billing_contract_invalid")
    if "billing_type" in (_patch_calls.get("payload") or {}) \
            or "billing_period" in (_patch_calls.get("payload") or {}):
        raise RuntimeError("fake: PATCH must not carry billing contract")
    _patch_calls["last"] = (product_id, price_cents, currency)
    _mirror_state[product_id] = {**prod, "price": price_cents,
                                 "currency": str(currency).upper()}
    return {"object": "product", "id": product_id, "price": price_cents,
            "currency": str(currency).upper(),
            "billing_type": "recurring", "billing_period": "every-month"}

def _fake_verify(product_id, price_cents, currency):
    prod = _mirror_state.get(product_id)
    if prod is None:
        return "not_found"
    if int(prod["price"]) != int(price_cents):
        return "price"
    if str(prod["currency"]).upper() != str(currency).upper():
        return "currency"
    if prod["billing_type"] != "recurring":
        return "billing_type"
    if prod.get("billing_period") != "every-month":
        return "billing_period"
    return ""

_patch_payloads = []
_orig_creem_key = _creem.CREEM_API_KEY
_orig_patch = _creem._patch
def _spy_patch(path, payload):
    _patch_payloads.append((path, dict(payload)))
    prod_id = path.rsplit("/", 1)[-1]
    prod = _mirror_state.get(prod_id)
    if prod is None:
        raise RuntimeError("product_not_found")
    if "billing_type" in payload or "billing_period" in payload:
        raise RuntimeError("patch_carries_billing_contract")
    if prod["billing_type"] != "recurring" or prod.get("billing_period") != "every-month":
        raise RuntimeError("product_billing_contract_invalid")
    _mirror_state[prod_id] = {**prod, "price": payload["price"],
                              "currency": payload["currency"]}
    return {"object": "product", "id": prod_id, "price": payload["price"],
            "currency": payload["currency"],
            "billing_type": "recurring", "billing_period": "every-month"}
_orig_fetch_prod = _creem._fetch_product
def _spy_fetch_prod(pid):
    prod = _mirror_state.get(pid)
    return dict(prod) if prod else None
# real mirror_product_price exercised against spied transport
_creem._patch = _spy_patch
_creem._fetch_product = _spy_fetch_prod
_creem.CREEM_API_KEY = "test-key"  # real impl gates on the module constant

_orig_creem = (_creem.mirror_product_price, _creem.verify_product_mirror,
               _creem.create_subscription_checkout, _creem.create_credits_checkout,
               _creem.creem_enabled, _creem.credits_ready,
               _creem.CREEM_CREDITS_CURRENCY)
_credits_chk = {"n": 0}
def _fake_sub_checkout(order_id, plan, user):
    # mirror the REAL contract: verify the product mirror before any
    # checkout session is created (fakes must not weaken the boundary)
    err = _fake_verify(plan["creem_product_id"], int(plan["price_cents"]),
                       str(plan.get("currency") or "USD"))
    if err:
        raise RuntimeError(f"price_mirror_mismatch:{err}")
    return {"ok": True, "checkout_url": f"https://creem.test/s/{order_id}",
            "checkout_id": f"ch_pc_{order_id}"}
def _fake_credits_checkout(order_id, cents, user):
    _credits_chk["n"] += 1
    return {"ok": True, "checkout_url": f"https://creem.test/t/{order_id}",
            "checkout_id": f"ch_pc_{order_id}"}
_fake_mirror_full = _fake_mirror  # kept for direct-call assertions
# route through the REAL implementation (transport spied) so the
# GET-first / PATCH-payload / re-confirm contract is actually exercised
_creem.verify_product_mirror = _fake_verify
_creem.create_subscription_checkout = _fake_sub_checkout
_creem.create_credits_checkout = _fake_credits_checkout
_creem.creem_enabled = lambda: True
_creem.credits_ready = lambda: True
_creem.CREEM_CREDITS_CURRENCY = "USD"

# ═══ P1: 518 price change writes through to Creem ═══
from app.web_auth import create_browser_session, COOKIE_NAME
from fastapi.testclient import TestClient
from app.main import app as _app
client_fwd = TestClient(_app)

op = _mkuser_fwd(_u_fwd + "op")
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (op,))
op_sess_fwd = create_browser_session(op)

# create a paid plan bound to prod_pc1 (mirror called at creation)
with db() as con:
    _so = int(con.execute(
        "SELECT COALESCE(MAX(p.sort_order),0)+1 AS so FROM plans p WHERE p.code <> 'free'"
    ).fetchone()["so"])
r = client_fwd.post("/samelabs/commerce/products/plans", json={
    "code": f"plus_{_u_fwd}", "name": "Plus PC", "monthly_credits": 500,
    "price_cents": 999, "creem_product_id": "prod_pc1", "sort_order": _so,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess_fwd})
check("P1 create bound plan → mirror write-through at creation",
      r.status_code == 200 and _mirror_state["prod_pc1"]["price"] == 999
      and len(_patch_payloads) == 1,
      f"{r.status_code} mirror={_mirror_state['prod_pc1']} n={len(_patch_payloads)}")
check("P1a write-through PATCH 只带 price/currency (不夹带 billing contract)",
      all(("billing_type" not in pl and "billing_period" not in pl)
          for _, pl in _patch_payloads), str(_patch_payloads[:2]))
plan_id = r.json()["plan"]["id"]

# price update → Creem PATCH first, then local commit
r = client_fwd.patch(f"/samelabs/commerce/products/plans/{plan_id}", json={"price_cents": 1499},
                 cookies={COOKIE_NAME: op_sess_fwd})
check("P1 改价 → Creem Product update 被调用",
      r.status_code == 200 and _mirror_state["prod_pc1"]["price"] == 1499
      and _patch_payloads[-1] == (f"/v1/products/prod_pc1",
                                  {"price": 1499, "currency": "USD"}),
      f"{r.status_code} last={_patch_payloads[-1:]}")
with db() as con:
    local_price = con.execute("select price_cents from plans where id=%s",
                              (plan_id,)).fetchone()["price_cents"]
check("P1 provider 成功 → local price commit (1499)", int(local_price) == 1499)

# non-commercial edit → NO Creem call
n0 = len(_patch_payloads)
r = client_fwd.patch(f"/samelabs/commerce/products/plans/{plan_id}", json={"name": "Plus PC2"},
                 cookies={COOKIE_NAME: op_sess_fwd})
check("P1 非商业字段修改不调用 Creem",
      r.status_code == 200 and len(_patch_payloads) == n0,
      f"{r.status_code} calls {len(_patch_payloads)}-{n0}")

# ═══ P2: Creem update fail → local price NOT committed ═══
_orig_spy = _creem._patch
def _failing_patch(path, payload):
    raise RuntimeError("provider down")
_creem._patch = _failing_patch
r = client_fwd.patch(f"/samelabs/commerce/products/plans/{plan_id}", json={"price_cents": 1999},
                 cookies={COOKIE_NAME: op_sess_fwd})
with db() as con:
    local_price2 = con.execute("select price_cents from plans where id=%s",
                               (plan_id,)).fetchone()["price_cents"]
check("P2 Creem 更新失败 → 502 + local price 不 commit",
      r.status_code == 502 and int(local_price2) == 1499,
      f"{r.status_code} price={local_price2}")
_creem._patch = _orig_spy

# ═══ P3/P4: checkout verification (mirror consistent → allowed; mismatch → fail closed) ═══
import urllib.parse as _up
st = client_fwd.post("/auth/register", data={
    "username": _u_fwd + "b", "email": f"{_u_fwd}b@t.co",
    "password": "password123", "password_confirm": "password123"},
    follow_redirects=False).status_code
lg = client_fwd.post("/auth/login", data={"login": _u_fwd + "b", "password": "password123"},
                 follow_redirects=False)
buyer_ck = (lg.cookies.get(COOKIE_NAME) or "")
with db() as con:
    buyer = int(con.execute(
        "select id from users where username=%s", (_u_fwd + "b",)).fetchone()["id"])
check("P0 buyer 注册+登录 ok", lg.status_code == 302 and buyer_ck, lg.status_code)

def _api(path, body):
    r = client_fwd.post(path, json=body, cookies={COOKIE_NAME: buyer_ck})
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}

# consistent mirror (1499 in both) → checkout allowed
st, d = _api("/api/topup/subscribe", {"plan_id": plan_id})
check("P3 provider/local 价格一致 → checkout allowed",
      st == 200 and d.get("ok"), f"{st} {str(d)[:90]}")

# mismatch: mirror drifts to 999 while local says 1499 → fail closed
# (second buyer: the first buyer's pending order must not mask the test)
_mirror_state["prod_pc1"]["price"] = 999
client_fwd.post("/auth/register", data={
    "username": _u_fwd + "b2", "email": f"{_u_fwd}b2@t.co",
    "password": "password123", "password_confirm": "password123"})
lg2 = client_fwd.post("/auth/login", data={"login": _u_fwd + "b2", "password": "password123"},
                  follow_redirects=False)
_ck2 = lg2.cookies.get(COOKIE_NAME) or ""
r_mis = client_fwd.post("/api/topup/subscribe",
                    json={"plan_id": plan_id},
                    cookies={COOKIE_NAME: _ck2})
st2, d2 = r_mis.status_code, r_mis.json()
check("P4 mirror mismatch → checkout fail closed (不按 Creem 价售卖)",
      st2 == 502 and "checkout_unresolved" in str(d2.get("error", "")),
      f"{st2} {str(d2)[:90]}")
with db() as con:
    local_price3 = con.execute("select price_cents from plans where id=%s",
                               (plan_id,)).fetchone()["price_cents"]
check("P4 mismatch 时 518 Plan 未被 checkout 路径改动",
      int(local_price3) == 1499, local_price3)

# recurring checkout payload has NO custom_price (source contract)
_src = open(_os.path.join(REPO, "core", "economy", "creem.py"), encoding="utf-8").read()
_sub_fn = _src[_src.index("def create_subscription_checkout"):
               _src.index("def upgrade_subscription")]
check("P5 subscription checkout payload 无 custom_price (源合同)",
      "custom_price" not in _sub_fn.replace("ZERO custom_price", ""))

# ═══ P6-P8: package credits snapshot ═══
with db() as con:
    pkg = con.execute(
        "insert into packages (code, name, credits, price_cents, currency,"
        " active, created_at, updated_at) values (%s, 'PC Pkg', 10000, 1000,"
        " 'USD', true, %s, %s) returning id",
        (f"pkg_{_u_fwd}", ts_fwd, ts_fwd)).fetchone()
    pkg_id = pkg["id"]
st, d = _api("/api/topup/checkout", {"package_id": pkg_id})
check("P6 package checkout ok + order 冻结 credits_snapshot=10000",
      st == 200 and d.get("ok"), f"{st} {str(d)[:90]}")
with db() as con:
    order = con.execute(
        "select id, credits_snapshot, status from orders where item_id=%s"
        " and kind='package' order by id desc limit 1", (pkg_id,)).fetchone()
check("P6 snapshot 字段落库 = 10000", int(order["credits_snapshot"]) == 10000,
      str(dict(order)))

# operator mutates the package AFTER checkout: 10000 → 20000 (price moves
# with it to stay inside the commercial floor: credits <= price_cents*10)
r = client_fwd.patch(f"/samelabs/commerce/products/packages/{pkg_id}",
                 json={"credits": 20000, "price_cents": 2000},
                 cookies={COOKIE_NAME: op_sess_fwd})
check("P7 operator 改 package → 20000 (live 行可变)", r.status_code == 200)

# fulfillment reads ONLY the snapshot → grants 10000, not 20000
# restore real checkout fakes boundary (only checkout fakes needed isolation)
_creem.mirror_product_price, _creem.verify_product_mirror = _orig_creem[0], _orig_creem[1]
_creem.create_subscription_checkout = _orig_creem[2]
_creem.create_credits_checkout = _orig_creem[3]
from economy.billing import spendable_balance
_bal0 = spendable_balance(buyer)
# set the shared product contract env pieces fulfill_by_request_id checks
_creem.CREEM_CREDITS_PRODUCT_ID = "prod_credits_shared"
# canonical fulfillment: the REAL local Order.id + canonical provider id
try:
    _creem.fulfill_by_request_id(
        int(order["id"]), provider_order_id=f"ord_pc_{order['id']}",
        checkout_id=f"ch_pc_{order['id']}",
        amount_cents=1000, product_id="prod_credits_shared",
        currency="USD")
    granted = spendable_balance(buyer) - _bal0
except Exception as e:
    granted = f"ERR {e}"
check("P7 fulfillment 只读 snapshot → grant 10000 (非 live 20000)",
      granted == 10000, str(granted))

# replay: SAME Order.id + SAME canonical provider_order_id + SAME checkout
# identity → guarded writer no-op, zero double grant
_bal1 = spendable_balance(buyer)
_replay = _creem.fulfill_by_request_id(
    int(order["id"]), provider_order_id=f"ord_pc_{order['id']}",
    checkout_id=f"ch_pc_{order['id']}",
    amount_cents=1000, product_id="prod_credits_shared",
    currency="USD")
check("P8 webhook replay 不 double grant",
      _replay.get("granted") is False and _replay.get("reason") == "replay"
      and spendable_balance(buyer) == _bal1,
      f"{_bal1} → {spendable_balance(buyer)} {_replay}")

# reverse direction: package raised BEFORE checkout → snapshot freezes high value
with db() as con:
    con.execute("update packages set credits=30000 where id=%s", (pkg_id,))
_creem.creem_enabled = lambda: True
_creem.credits_ready = lambda: True
_creem.create_credits_checkout = _fake_credits_checkout
st, d = _api("/api/topup/checkout", {"package_id": pkg_id})
with db() as con:
    order2 = con.execute(
        "select id, credits_snapshot from orders where item_id=%s"
        " and kind='package' order by id desc limit 1", (pkg_id,)).fetchone()
check("P6b 反向: 改价后 checkout 冻结新值 30000",
      st == 200 and int(order2["credits_snapshot"]) == 30000,
      f"st={st} order={dict(order2)}")

# amount/currency/product fail-closed (existing behavior kept)
with db() as con:
    _prov2 = con.execute("select provider_order_id from orders where id=%s",
                         (order2["id"],)).fetchone()["provider_order_id"]
_ch2 = _prov2[3:] if str(_prov2).startswith("ch:") else f"ch_pc_{order2['id']}"
try:
    _creem.fulfill_by_request_id(
        int(order2["id"]), provider_order_id=f"ord_pc_{order2['id']}",
        checkout_id=_ch2,
        amount_cents=999, product_id="prod_credits_shared",
        currency="USD")
    check("P8b amount mismatch fail closed", False, "no raise")
except KeyError as e:
    check("P8b amount mismatch fail closed", "amount_mismatch" in str(e), str(e))

# ═══ P9-P16: paid-period transaction proof ═══
from economy.subscription import handle_event, _parse_ts
from repo import orders as orders_repo

PS_fwd, PE_fwd = ts_fwd - 86400, ts_fwd + 86400 * 29
def _iso_fwd(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

member = _mkuser_fwd(_u_fwd + "m")
sub_id_fwd = f"sub_pc_{_u_fwd}"
with db() as con:
    prows = con.execute("select id from plans where id=%s", (plan_id,)).fetchone()
    oid = con.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, paid_at, created_at) values"
        " (%s, 'plan', %s, 'creem', %s, 'paid', 1499, 'USD', %s, %s)"
        " returning id",
        (member, plan_id, "sub:" + sub_id_fwd, ts_fwd, ts_fwd)).fetchone()["id"]
    con.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id,"
        " status, started_at, renews_at, updated_at) values (%s, %s, %s,"
        " 'active', %s, %s, %s)",
        (member, plan_id, "sub:" + sub_id_fwd, PS_fwd, PE_fwd, ts_fwd))
    con.execute("update plans set monthly_credits=100 where id=%s", (plan_id,))

def _paid_event(with_txn="txn_ok", period=(PS_fwd, PE_fwd), sub=None, status="active",
                product="prod_pc1", include_txn_field=True):
    ssub = {
        "id": sub or sub_id_fwd, "status": status,
        "current_period_start_date": _iso_fwd(period[0]),
        "current_period_end_date": _iso_fwd(period[1]),
        "product": {"id": product},
    }
    if include_txn_field:
        ssub["last_transaction_id"] = with_txn
    return ("subscription.paid", {"object": "subscription", **ssub})

def _run_paid(**kw):
    et, obj = _paid_event(**kw)
    try:
        return {"ok": handle_event(et, obj)}
    except KeyError as e:
        return {"err": str(e)}

# P9: complete proof → grants exactly once
_bal2 = spendable_balance(member)
_txns["txn_ok"] = {"object": "transaction", "id": "txn_ok", "status": "paid",
                   "type": "invoice", "subscription": sub_id_fwd,
                   "period_start": PS_fwd, "period_end": PE_fwd}
r = _run_paid()
_bal3 = spendable_balance(member)
check("P9 subscription.paid 完整 proof → grant 一次 (100)",
      "ok" in r and _bal3 - _bal2 == 100, f"{r} Δ={_bal3 - _bal2}")

# P10: replay does not double grant
r = _run_paid()
check("P10 paid replay 不 double grant",
      spendable_balance(member) == _bal3, str(spendable_balance(member) - _bal3))

# P11: missing proof fields → fail loud (KeyError), no grant
_bal4 = spendable_balance(member)
r = _run_paid(include_txn_field=False)
check("P11 缺 last_transaction_id → fail loud 不 grant",
      "err" in r and "paid_proof_missing" in r["err"]
      and spendable_balance(member) == _bal4, r)

# ═══ reconcile proofs (P12-P16) ═══
def _rec_setup(txn_payload, sub_extra=None, sub_status="active",
               period=(PS_fwd, PE_fwd), sub=sub_id_fwd):
    snap = {"object": "subscription", "id": sub, "status": sub_status,
            "current_period_start_date": _iso_fwd(period[0]),
            "current_period_end_date": _iso_fwd(period[1]),
            "product": {"id": "prod_pc1"},
            "last_transaction_id": txn_payload["id"] if txn_payload else ""}
    if sub_extra:
        snap.update(sub_extra)
    _rec_fwd.fetch_subscription = (lambda _s, _snap=snap: _snap)
    _rec_fwd.fetch_transaction = (lambda _t, _p=txn_payload: _p)

member2 = _mkuser_fwd(_u_fwd + "m2")
sub2 = f"sub_pc2_{_u_fwd}"
with db() as con:
    oid2 = con.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, paid_at, created_at) values"
        " (%s, 'plan', %s, 'creem', %s, 'paid', 1499, 'USD', %s, %s)"
        " returning id",
        (member2, plan_id, "sub:" + sub2, ts_fwd, ts_fwd)).fetchone()["id"]
    con.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id,"
        " status, started_at, renews_at, updated_at) values (%s, %s, %s,"
        " 'active', %s, %s, %s)",
        (member2, plan_id, "sub:" + sub2, PS_fwd, PE_fwd, ts_fwd))

_txid = 0
def _txn(status="paid", typ="invoice", subscription=None, ps=PS_fwd, pe=PE_fwd):
    global _txid
    _txid += 1
    return {"object": "transaction", "id": f"txn_r{_txid}", "status": status,
            "type": typ, "subscription": subscription or sub2,
            "period_start": ps, "period_end": pe}

# P12: matching paid invoice → reconcile grants
_bal5 = spendable_balance(member2)
_rec_setup(_txn())
r = _rec_fwd.reconcile_user(member2)
check("P12 reconcile + matching paid invoice → grant",
      r["granted"] == 1 and spendable_balance(member2) - _bal5 == 100, f"{r}")

# P13: reconcile after webhook does not double grant
_rec_setup(_txn())
r = _rec_fwd.reconcile_user(member2)
check("P13 reconcile 在 webhook 后不 double grant",
      r["granted"] == 0, f"{r}")

# P14: negative transaction states never grant
member3 = _mkuser_fwd(_u_fwd + "m3")
sub3 = f"sub_pc3_{_u_fwd}"
with db() as con:
    con.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, paid_at, created_at) values"
        " (%s, 'plan', %s, 'creem', %s, 'paid', 1499, 'USD', %s, %s)",
        (member3, plan_id, "sub:" + sub3, ts_fwd, ts_fwd))
    con.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id,"
        " status, started_at, renews_at, updated_at) values (%s, %s, %s,"
        " 'active', %s, %s, %s)",
        (member3, plan_id, "sub:" + sub3, PS_fwd, PE_fwd, ts_fwd))
_orig_fetch_sub, _orig_fetch_txn = _rec_fwd.fetch_subscription, _rec_fwd.fetch_transaction
def _rec3(txn, sub_status="active", sub=None):
    _s = sub or sub3
    _rec_fwd.fetch_subscription = lambda _x, _s=_s, _st=sub_status: {
        "object": "subscription", "id": _s, "status": _st,
        "current_period_start_date": _iso_fwd(PS_fwd), "current_period_end_date": _iso_fwd(PE_fwd),
        "product": {"id": "prod_pc1"},
        "last_transaction_id": (txn or {}).get("id", "")}
    _rec_fwd.fetch_transaction = lambda _t, _p=txn: _p

for status in ("pending", "refunded", "partialRefund", "chargedBack"):
    _rec3(_txn(status=status))
    r = _rec_fwd.reconcile_user(member3)
    check(f"P14 active + transaction {status} → 不 grant",
          r["granted"] == 0, str(r))
# wrong subscription
_rec3(_txn(subscription="sub_someone_else"))
r = _rec_fwd.reconcile_user(member3)
check("P14 transaction 属其它 subscription → 不 grant", r["granted"] == 0, str(r))
# wrong period
_rec3(_txn(ps=PS_fwd - 86400, pe=PE_fwd - 86400))
r = _rec_fwd.reconcile_user(member3)
check("P14 transaction period 不匹配 → 不 grant", r["granted"] == 0, str(r))
# missing transaction
_rec3(None)
r = _rec_fwd.reconcile_user(member3)
check("P14 transaction 缺失 → 不 grant", r["granted"] == 0, str(r))
# wrong type (payment, not invoice)
_rec3(_txn(typ="payment"))
r = _rec_fwd.reconcile_user(member3)
check("P14 transaction type=payment (非 invoice) → 不 grant",
      r["granted"] == 0, str(r))
# active status alone (no txn id at all)
_rec_fwd.fetch_subscription = lambda _x: {
    "object": "subscription", "id": sub3, "status": "active",
    "current_period_start_date": _iso_fwd(PS_fwd), "current_period_end_date": _iso_fwd(PE_fwd),
    "product": {"id": "prod_pc1"}}
r = _rec_fwd.reconcile_user(member3)
check("P14 active status 本身绝不 grant (无 proof)", r["granted"] == 0, str(r))

# P15: scheduled_cancel + matching paid transaction heals a lost webhook
member4 = _mkuser_fwd(_u_fwd + "m4")
sub4 = f"sub_pc4_{_u_fwd}"
with db() as con:
    con.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, paid_at, created_at) values"
        " (%s, 'plan', %s, 'creem', %s, 'paid', 1499, 'USD', %s, %s)",
        (member4, plan_id, "sub:" + sub4, ts_fwd, ts_fwd))
    # local row stays 'active' (DB CHECK vocabulary) with the lifecycle
    # marker set — reconcile's eligibility looks at the PROVIDER snapshot
    con.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id,"
        " status, started_at, renews_at, updated_at) values (%s, %s, %s,"
        " 'active', %s, %s, %s)",
        (member4, plan_id, "sub:" + sub4, PS_fwd, PE_fwd, ts_fwd))
_rec_fwd.fetch_subscription = lambda _x: {
    "object": "subscription", "id": sub4, "status": "scheduled_cancel",
    "current_period_start_date": _iso_fwd(PS_fwd), "current_period_end_date": _iso_fwd(PE_fwd),
    "product": {"id": "prod_pc1"}, "last_transaction_id": "txn_sc"}
_rec_fwd.fetch_transaction = lambda _t: {
    "object": "transaction", "id": "txn_sc", "status": "paid",
    "type": "invoice", "subscription": sub4,
    "period_start": PS_fwd, "period_end": PE_fwd}
_bal6 = spendable_balance(member4)
r = _rec_fwd.reconcile_user(member4)
check("P15 scheduled_cancel + matching paid txn → 补授丢失 webhook",
      r["granted"] == 1 and spendable_balance(member4) - _bal6 == 100, str(r))

# P16: provider failure → best-effort skip, never grant, never raise
_rec_fwd.fetch_subscription = lambda _x: None
try:
    r = _rec_fwd.reconcile_user(member4)
    check("P16 provider 失败 → 不抛不 grant", r["granted"] == 0, str(r))
except Exception as e:
    check("P16 provider 失败 → 不抛不 grant", False, str(e))

# ═══ PM2-1: billing period in the mirror contract ═══
# (create_subscription_checkout was restored to the real implementation
# after the package section; re-fake it with the mirror-verified fake)
_creem.create_subscription_checkout = _fake_sub_checkout
_mirror_state["prod_pc1"]["price"] = 1499  # same price/currency, yearly period
_mirror_state["prod_pc1"]["billing_period"] = "every-year"
r_y = client_fwd.post("/api/topup/subscribe",
                  json={"plan_id": plan_id},
                  cookies={COOKIE_NAME: buyer_ck})
# buyer already has a pending order → use a fresh buyer for cleanliness
client_fwd.post("/auth/register", data={
    "username": _u_fwd + "y1", "email": f"{_u_fwd}y1@t.co",
    "password": "password123", "password_confirm": "password123"})
_lgy = client_fwd.post("/auth/login", data={"login": _u_fwd + "y1", "password": "password123"},
                   follow_redirects=False)
_cky = _lgy.cookies.get(COOKIE_NAME) or ""
r_y = client_fwd.post("/api/topup/subscribe",
                  json={"plan_id": plan_id},
                  cookies={COOKIE_NAME: _cky})
check("PM2-1a every-year + 相同 price/currency → checkout rejected",
      r_y.status_code == 502, r_y.status_code)
_mirror_state["prod_pc1"]["billing_period"] = "every-month"
# Canonical retry semantics: the PM2-1a attempt ended unresolved
# (mirror rejection is a pre-provider failure, order stays pending), so
# the SAME logical purchase retries through the SAME pending Order and
# must now succeed on it.
r_m = client_fwd.post("/api/topup/subscribe",
                  json={"plan_id": plan_id},
                  cookies={COOKIE_NAME: _cky})
check("PM2-1b every-month + same-ref retry → checkout allowed",
      r_m.status_code == 200 and r_m.json().get("ok"),
      f"{r_m.status_code} {r_m.text[:120]}")

# ═══ PM2-2: write-through never repairs billing contract ═══
_mirror_state["prod_pc2"] = {"price": 500, "currency": "USD",
                             "billing_type": "onetime",
                             "billing_period": ""}
_n_before = len(_patch_payloads)
try:
    # call the REAL implementation against the spied transport
    import economy.creem as _cmod
    _cmod.mirror_product_price("prod_pc2", 1499, "USD")
    pm2_ok = False
except RuntimeError as e:
    pm2_ok = "billing_contract" in str(e)
check("PM2-2 one-time product 改价 → fail closed (不 PATCH 不修复)",
      pm2_ok and len(_patch_payloads) == _n_before,
      f"patches {len(_patch_payloads)}-{_n_before}")
_mirror_state["prod_pc3"] = {"price": 500, "currency": "USD",
                             "billing_type": "recurring",
                             "billing_period": "every-year"}
_n_before = len(_patch_payloads)
try:
    import economy.creem as _cmod
    _cmod.mirror_product_price("prod_pc3", 1499, "USD")
    pm2b_ok = False
except RuntimeError as e:
    pm2b_ok = "billing_contract" in str(e)
check("PM2-2b yearly recurring product 改价 → fail closed",
      pm2b_ok and len(_patch_payloads) == _n_before)
# PATCH payload never carries billing fields (all recorded payloads)
check("PM2-2c 所有 PATCH payload 只含 price/currency",
      all(set(pl.keys()) == {"price", "currency"} for _, pl in _patch_payloads),
      str(_patch_payloads[:2]))

# ═══ PM2-3: creem_product_id change closes at the operator write ═══
# Rebind is legal ONLY on a plan with NO bound membership (lifecycle
# guard) — the subscription-carrying plan above must be refused. Use a
# fresh never-subscribed plan for the commit/rollback matrix, and prove
# the guard on the bound one first.
r_guard = client_fwd.patch(f"/samelabs/commerce/products/plans/{plan_id}",
                       json={"creem_product_id": f"prod_pcB_{_u_fwd}"},
                       cookies={COOKIE_NAME: op_sess_fwd})
check("PM2-3g rebind on bound-membership plan → 409 product_binding_immutable",
      r_guard.status_code == 409
      and "product_binding_immutable" in str(r_guard.json().get("error", "")),
      f"{r_guard.status_code} {r_guard.text[:90]}")
with db() as con:
    _so2 = con.execute(
        "SELECT COALESCE(MAX(p.sort_order),0)+1 AS so FROM plans p"
        " WHERE p.code <> 'free'").fetchone()["so"]
from repo.orders import insert_plan as _ins_plan
with db() as con:
    _rb = _ins_plan(con, f"plus_{_u_fwd}rb", "Plus RB", 500, 1499, ts_fwd,
                    active=True,
                    sort_order=int(_so2), creem_product_id=f"prod_pcA_{_u_fwd}",
                    ingress_files_24h=500, ingress_bytes_24h=524288000,
                    expiry_days=(7,), download_limits=(50,))
    rb_plan_id = int(_rb["id"])
from governance import plan_catalog as _pc
_pc.catalog(refresh=True)   # operator create path refreshes; mirror here
# target monthly product at a different price → binding syncs target to CURRENT 518 price
_mirror_state[f"prod_pcB_{_u_fwd}"] = {"price": 999, "currency": "USD",
                             "billing_type": "recurring",
                             "billing_period": "every-month"}
r = client_fwd.patch(f"/samelabs/commerce/products/plans/{rb_plan_id}", json={"creem_product_id": f"prod_pcB_{_u_fwd}"},
                 cookies={COOKIE_NAME: op_sess_fwd})
check("PM2-3a product rebind A→B → 409 forbidden (binding immutable)",
      r.status_code == 409
      and "product_binding_immutable" in str(r.json().get("error", ""))
      and _mirror_state[f"prod_pcB_{_u_fwd}"]["price"] == 999,   # provider untouched
      f"{r.status_code} {r.text[:90]} target={_mirror_state.get('prod_pcB_' + _u_fwd)}")
# yearly target → local binding not committed
_mirror_state[f"prod_pcY_{_u_fwd}"] = {"price": 999, "currency": "USD",
                             "billing_type": "recurring",
                             "billing_period": "every-year"}
r = client_fwd.patch(f"/samelabs/commerce/products/plans/{rb_plan_id}", json={"creem_product_id": f"prod_pcY_{_u_fwd}"},
                 cookies={COOKIE_NAME: op_sess_fwd})
with db() as con:
    bound = con.execute("select creem_product_id from plans where id=%s",
                        (rb_plan_id,)).fetchone()["creem_product_id"]
check("PM2-3b yearly rebind → 409 (仍 prod_pcA)",
      r.status_code == 409 and bound == f"prod_pcA_{_u_fwd}", f"{r.status_code} bound={bound}")
# provider failure → binding not committed
def _fail_fetch(pid):
    return None
_orig_fp = _creem._fetch_product
_creem._fetch_product = _fail_fetch
r = client_fwd.patch(f"/samelabs/commerce/products/plans/{rb_plan_id}", json={"creem_product_id": "prod_pcC"},
                 cookies={COOKIE_NAME: op_sess_fwd})
with db() as con:
    bound2 = con.execute("select creem_product_id from plans where id=%s",
                         (rb_plan_id,)).fetchone()["creem_product_id"]
check("PM2-3c provider failure rebind → 409 (仍 prod_pcA)",
      r.status_code == 409 and bound2 == f"prod_pcA_{_u_fwd}", f"{r.status_code} bound={bound2}")
_creem._fetch_product = _orig_fp
# valid monthly target → binding succeeds
_mirror_state["prod_pcD"] = {"price": 100, "currency": "USD",
                             "billing_type": "recurring",
                             "billing_period": "every-month"}
r = client_fwd.patch(f"/samelabs/commerce/products/plans/{rb_plan_id}", json={"creem_product_id": "prod_pcD"},
                 cookies={COOKIE_NAME: op_sess_fwd})
with db() as con:
    bound3 = con.execute("select creem_product_id from plans where id=%s",
                         (rb_plan_id,)).fetchone()["creem_product_id"]
check("PM2-3d valid monthly rebind → 409 (binding immutable; 仅 NULL→X 合法)",
      r.status_code == 409 and bound3 == f"prod_pcA_{_u_fwd}"
      and _mirror_state["prod_pcD"]["price"] == 100,
      f"{r.status_code} bound={bound3}")

# ═══ PM2-4: paid-period product identity proof ═══
def _mk_member_sub(uname, target_plan_id, product_binding):
    """seed user + order + membership for product-identity cases"""
    mm = _mkuser_fwd(uname)
    ss = f"sub_pi_{uname}"
    with db() as con:
        con.execute(
            "insert into orders (user_id, kind, item_id, provider,"
            " provider_order_id, status, amount_cents, currency, paid_at,"
            " created_at) values (%s, 'plan', %s, 'creem', %s, 'paid',"
            " 1499, 'USD', %s, %s)",
            (mm, target_plan_id, "sub:" + ss, ts_fwd, ts_fwd))
        con.execute(
            "insert into memberships (user_id, plan_id, provider_subscription_id,"
            " status, started_at, renews_at, updated_at) values (%s, %s, %s,"
            " 'active', %s, %s, %s)",
            (mm, target_plan_id, "sub:" + ss, PS_fwd, PE_fwd, ts_fwd))
        con.execute("update plans set creem_product_id=%s where id=%s",
                    (product_binding, target_plan_id))
    return mm, ss

def _paid_evt(ss, product, with_txn=True):
    ssub = {"object": "subscription", "id": ss, "status": "active",
            "current_period_start_date": _iso_fwd(PS_fwd),
            "current_period_end_date": _iso_fwd(PE_fwd),
            "product": {"id": product}}
    if with_txn:
        ssub["last_transaction_id"] = f"txn_{ss}"
    return ("subscription.paid", ssub)

def _try_paid(ss, product):
    et, obj = _paid_evt(ss, product)
    try:
        return {"ok": handle_event(et, obj)}
    except KeyError as e:
        return {"err": str(e)}

# a second tier for mismatch cases
with db() as con:
    _so2 = int(con.execute(
        "SELECT COALESCE(MAX(p.sort_order),0)+1 AS so FROM plans p WHERE p.code <> 'free'"
    ).fetchone()["so"])
_mirror_state["prod_pi_pro"] = {"price": 2999, "currency": "USD",
                                "billing_type": "recurring",
                                "billing_period": "every-month"}
r2 = client_fwd.post("/samelabs/commerce/products/plans", json={
    "code": f"pro_{_u_fwd}", "name": "Pro PI", "monthly_credits": 2000,
    "price_cents": 2999, "creem_product_id": "prod_pi_pro", "sort_order": _so2,
    "ingress_files_24h": 2000, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess_fwd})
plan2_id = r2.json()["plan"]["id"]

# normal current product → grant
mmA, ssA = _mk_member_sub(_u_fwd + "pa", plan_id, "prod_pi_plus")
_bA = spendable_balance(mmA)
rr = _try_paid(ssA, "prod_pi_plus")
check("PM2-4a normal current product → grant",
      "ok" in rr and spendable_balance(mmA) - _bA == 100, str(rr))

# unknown product → no grant, fail loud
mmB, ssB = _mk_member_sub(_u_fwd + "pb", plan_id, "prod_pi_plus")
_bB = spendable_balance(mmB)
rr = _try_paid(ssB, "prod_totally_unknown")
check("PM2-4b unknown product → no grant (fail loud)",
      "paid_product_unknown" in rr.get("err", "") and spendable_balance(mmB) == _bB,
      str(rr))

# unrelated known product (same or other tier but bound to another plan) →
# higher tier before update sync → no grant
mmC, ssC = _mk_member_sub(_u_fwd + "pc", plan_id, "prod_pi_plus")
_bC = spendable_balance(mmC)
rr = _try_paid(ssC, "prod_pi_pro")   # higher tier, Membership not synced
check("PM2-4c higher product before update sync → no grant (retriable)",
      "paid_product_not_synced" in rr.get("err", "")
      and spendable_balance(mmC) == _bC, str(rr))

# update sync then paid replay → correct grant at the new tier
_lg2u = client_fwd  # membership sync via subscription.update handler
_upd = {"object": "subscription", "id": ssC, "status": "active",
        "current_period_start_date": _iso_fwd(PS_fwd), "current_period_end_date": _iso_fwd(PE_fwd),
        "product": {"id": "prod_pi_pro"}, "last_transaction_id": "txn_upd"}
try:
    handle_event("subscription.update", _upd)
    synced = True
except Exception:
    synced = False
rr = _try_paid(ssC, "prod_pi_pro")
check("PM2-4d update sync 后 paid replay → 正确按新 tier grant",
      synced and "ok" in rr, f"synced={synced} rr={rr}")

# first-period order product mismatch → no grant (delete membership to hit order path)
mmD, ssD = _mk_member_sub(_u_fwd + "pd", plan_id, "prod_pi_plus")
with db() as con:
    con.execute("delete from memberships where user_id=%s", (mmD,))
_bD = spendable_balance(mmD)
rr = _try_paid(ssD, "prod_pi_pro")   # order says plus, provider says pro
check("PM2-4e first-period order product mismatch → no grant",
      "paid_product_order_mismatch" in rr.get("err", "")
      and spendable_balance(mmD) == _bD, str(rr))

# ═══ PM3: local constraints settle BEFORE any Creem side effect ═══
# fixtures: two plans, two products
_mirror_state["prod_sA"] = {"price": 999, "currency": "USD",
                            "billing_type": "recurring",
                            "billing_period": "every-month"}
_mirror_state["prod_sB"] = {"price": 1999, "currency": "USD",
                            "billing_type": "recurring",
                            "billing_period": "every-month"}
with db() as con:
    _soA = int(con.execute(
        "SELECT COALESCE(MAX(p.sort_order),0)+1 AS so FROM plans p WHERE p.code <> 'free'"
    ).fetchone()["so"])
rA = client_fwd.post("/samelabs/commerce/products/plans", json={
    "code": f"plA_{_u_fwd}", "name": "A", "monthly_credits": 100,
    "price_cents": 999, "creem_product_id": "prod_sA", "sort_order": _soA,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess_fwd})
planA = rA.json()["plan"]["id"]
rB = client_fwd.post("/samelabs/commerce/products/plans", json={
    "code": f"plB_{_u_fwd}", "name": "B", "monthly_credits": 2000,
    "price_cents": 1999, "creem_product_id": "prod_sB", "sort_order": _soA + 1,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess_fwd})
planB = rB.json()["plan"]["id"]

# PM3-1: rebind planA to planB's product → 409, zero PATCH, prod_sB price intact
_n1 = len(_patch_payloads)
r = client_fwd.patch(f"/samelabs/commerce/products/plans/{planA}", json={"creem_product_id": "prod_sB"},
                 cookies={COOKIE_NAME: op_sess_fwd})
with db() as con:
    _bindA = con.execute("select creem_product_id from plans where id=%s",
                         (planA,)).fetchone()["creem_product_id"]
check("PM3-1 rebind A→B → 409 product_binding_immutable / 零 PATCH / 原价不变",
      r.status_code == 409
      and (r.json().get("error") == "product_binding_immutable")
      and len(_patch_payloads) == _n1
      and _bindA == "prod_sA"
      and _mirror_state["prod_sB"]["price"] == 1999,
      f"{r.status_code} patches {len(_patch_payloads)}-{_n1} bind={_bindA} "
      f"prod_sB={_mirror_state['prod_sB']['price']}")

# PM3-2: create with an already-bound product → 409, zero PATCH
_n2 = len(_patch_payloads)
r = client_fwd.post("/samelabs/commerce/products/plans", json={
    "code": f"plC_{_u_fwd}", "name": "C", "monthly_credits": 100,
    "price_cents": 500, "creem_product_id": "prod_sB", "sort_order": _soA + 2,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess_fwd})
with db() as con:
    _cnt = con.execute("select count(*) c from plans where code=%s",
                       (f"plC_{_u_fwd}",)).fetchone()["c"]
check("PM3-2 create 用已占用 product → 409 / 零 PATCH / 行未落",
      r.status_code == 409 and r.json().get("error") == "product_already_bound"
      and len(_patch_payloads) == _n2 and int(_cnt) == 0,
      f"{r.status_code} patches {len(_patch_payloads)}-{_n2} cnt={_cnt}")

# PM3-3: create duplicate sort / duplicate code → 409, zero PATCH
_n3 = len(_patch_payloads)
r = client_fwd.post("/samelabs/commerce/products/plans", json={
    "code": f"plD_{_u_fwd}", "name": "D", "monthly_credits": 100,
    "price_cents": 500, "creem_product_id": "prod_sD", "sort_order": _soA,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess_fwd})
_mirror_state["prod_sD"] = {"price": 1, "currency": "USD",
                            "billing_type": "recurring",
                            "billing_period": "every-month"}
r2 = client_fwd.post("/samelabs/commerce/products/plans", json={
    "code": f"plA_{_u_fwd}", "name": "A2", "monthly_credits": 100,
    "price_cents": 500, "creem_product_id": "prod_sD", "sort_order": _soA + 3,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess_fwd})
check("PM3-3 create duplicate sort/code → 409 / 零 PATCH",
      r.status_code == 409 and r.json().get("error") == "sort_order_taken"
      and r2.status_code == 409 and r2.json().get("error") == "code_taken"
      and len(_patch_payloads) == _n3,
      f"sort:{r.status_code} code:{r2.status_code} patches {len(_patch_payloads)}-{_n3}")

# PM3-4: valid commercial update → provisional constraints → PATCH → commit
_n4 = len(_patch_payloads)
r = client_fwd.patch(f"/samelabs/commerce/products/plans/{planA}", json={"price_cents": 1099},
                 cookies={COOKIE_NAME: op_sess_fwd})
with db() as con:
    _pA = con.execute("select price_cents from plans where id=%s",
                      (planA,)).fetchone()["price_cents"]
check("PM3-4 valid 改价 → Creem PATCH + DB commit",
      r.status_code == 200 and len(_patch_payloads) == _n4 + 1
      and _patch_payloads[-1][1]["price"] == 1099 and int(_pA) == 1099,
      f"{r.status_code} patches {len(_patch_payloads)}-{_n4} pA={_pA}")

# PM3-5: provider failure → DB rollback, plan fully unchanged
_orig_patch_fn = _creem._patch
def _boom_patch(path, payload):
    raise RuntimeError("provider down")
_creem._patch = _boom_patch
r = client_fwd.patch(f"/samelabs/commerce/products/plans/{planA}", json={"price_cents": 2099},
                 cookies={COOKIE_NAME: op_sess_fwd})
with db() as con:
    _pA2 = con.execute(
        "select price_cents, creem_product_id from plans where id=%s",
        (planA,)).fetchone()
check("PM3-5 Creem 失败 → DB rollback, Plan 完全不变",
      r.status_code == 502 and int(_pA2["price_cents"]) == 1099
      and _pA2["creem_product_id"] == "prod_sA",
      f"{r.status_code} plan={dict(_pA2)}")
# create with provider failure → insert rolled back
r = client_fwd.post("/samelabs/commerce/products/plans", json={
    "code": f"plE_{_u_fwd}", "name": "E", "monthly_credits": 100,
    "price_cents": 700, "creem_product_id": "prod_sD", "sort_order": _soA + 4,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess_fwd})
with db() as con:
    _cntE = con.execute("select count(*) c from plans where code=%s",
                        (f"plE_{_u_fwd}",)).fetchone()["c"]
check("PM3-5b create Creem 失败 → insert rollback",
      r.status_code == 502 and int(_cntE) == 0,
      f"{r.status_code} cnt={_cntE}")
_creem._patch = _orig_patch_fn

# PM3-6: valid rebind → target mirror + local binding converge together
_mirror_state["prod_sF"] = {"price": 1, "currency": "USD",
                            "billing_type": "recurring",
                            "billing_period": "every-month"}
r = client_fwd.patch(f"/samelabs/commerce/products/plans/{planA}", json={"creem_product_id": "prod_sF"},
                 cookies={COOKIE_NAME: op_sess_fwd})
with db() as con:
    _bindA2 = con.execute("select creem_product_id from plans where id=%s",
                          (planA,)).fetchone()["creem_product_id"]
check("PM3-6 rebind → 409 forbidden (binding immutable; mirror 不动)",
      r.status_code == 409 and _bindA2 == "prod_sA"
      and _mirror_state["prod_sF"]["price"] == 1,
      f"{r.status_code} bind={_bindA2} mirror={_mirror_state['prod_sF']['price']}")

# ═══ PM4: forced-concurrency — mirror reads the in-tx final row ═══
# Reproduces the stale-outer-read race EXACTLY:
#   initial plan = 999 / prod_sA
#   B: enters update_plan() but STALLS after the outer read, holding a
#      pre-transaction snapshot of 999 (the old bug source)
#   A: commits price 999 → 1499 while B is stalled
#   B: proceeds — UPDATE product → prod_sB, then mirror
# With the fix, B mirrors prod_sB → 1499 (the row B's tx commits),
# NOT 999 (B's stale outer read).
_mirror_state["prod_cA"] = {"price": 999, "currency": "USD",
                            "billing_type": "recurring",
                            "billing_period": "every-month"}
_mirror_state["prod_cB"] = {"price": 1, "currency": "USD",
                            "billing_type": "recurring",
                            "billing_period": "every-month"}
with db() as con:
    _soC = int(con.execute(
        "SELECT COALESCE(MAX(p.sort_order),0)+1 AS so FROM plans p WHERE p.code <> 'free'"
    ).fetchone()["so"])
rC = client_fwd.post("/samelabs/commerce/products/plans", json={
    "code": f"plC1_{_u_fwd}", "name": "C1", "monthly_credits": 100,
    "price_cents": 999, "creem_product_id": "prod_cA", "sort_order": _soC,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess_fwd})
planC = rC.json()["plan"]["id"]

import threading
# gate: B's transaction starts, then pauses AFTER its outer read +
# BEFORE its UPDATE takes the row lock — A's full request commits in
# that window.
_b_entered = threading.Event()
_b_release = threading.Event()
_mirror_lock = threading.Lock()

_orig_update_plan_mod = None
from repo import orders as _orders_mod
_orig_update_plan = _orders_mod.update_plan

def _gated_update_plan(con, plan_id_, fields_, ts_):
    # runs INSIDE the route's transaction: signal, then wait until A has
    # fully committed, so B's UPDATE takes the row lock only afterwards
    _b_entered.set()
    _b_release.wait(timeout=30)
    return _orig_update_plan(con, plan_id_, fields_, ts_)

import app.routes.samelabs as _samelabs_mod
# the route imports update_plan at function scope from repo.orders —
# patch the module attribute it resolves at call time
_orders_mod.update_plan = _gated_update_plan

result_b = {}
def _worker_b():
    # price-only concurrency (rebind forbidden by binding immutability)
    result_b["resp"] = client_fwd.patch(
        f"/samelabs/commerce/products/plans/{planC}", json={"price_cents": 799},
        cookies={COOKIE_NAME: op_sess_fwd})

tb = threading.Thread(target=_worker_b)
tb.start()
check("PM4 setup: B 进入事务并停在 row lock 前", _b_entered.wait(timeout=10))

# A: full price update 999 → 1499, commits completely while B waits
ra = client_fwd.patch(f"/samelabs/commerce/products/plans/{planC}", json={"price_cents": 1499},
                  cookies={COOKIE_NAME: op_sess_fwd})
check("PM4 A 改价 commit (999→1499)", ra.status_code == 200, ra.status_code)

# release B: its UPDATE product → prod_cB runs under the row lock
_b_release.set()
tb.join(timeout=30)
_orders_mod.update_plan = _orig_update_plan

with db() as con:
    _fin = con.execute("select price_cents, creem_product_id from plans where id=%s",
                       (planC,)).fetchone()
check("PM4 并发终态: 最后提交者胜 (A 1499) / binding 不变",
      int(_fin["price_cents"]) == 1499 and _fin["creem_product_id"] == "prod_cA",
      str(dict(_fin)))
check("PM4 mirror prod_cA = 1499 (A 事务内 final row, 绝非 stale 999)",
      _mirror_state["prod_cA"]["price"] == 1499,
      str(_mirror_state["prod_cA"]))
check("PM4 B 请求本身成功 (price-only)", result_b.get("resp", None) is not None
      and result_b["resp"].status_code == 200,
      str(result_b.get("resp"))[:80])

# cleanup PM4 plan
with db() as con:
    con.execute("delete from plans where id=%s", (planC,))

# ═══ semantic guards ═══
_csrc = open(_os.path.join(REPO, "core", "economy", "creem.py"), encoding="utf-8").read()
check("S1 fulfillment 不再读 live package credits (源内无 package_credits 调用)",
      "package_credits" not in _csrc)
_rsrc = open(_os.path.join(REPO, "core", "economy", "reconcile.py"), encoding="utf-8").read()
check("S2 reconcile 有 transaction proof 谓词",
      "transaction_proves_paid_period" in _rsrc and "last_transaction_id" in _rsrc)
_ssrc = open(_os.path.join(REPO, "core", "economy", "subscription.py"), encoding="utf-8").read()
check("S3 paid-period writer 仍只有一个 implementation (apply_paid_period)",
      _ssrc.count("def apply_paid_period") == 1)

# ═══ restore fakes ═══
(_creem.mirror_product_price, _creem.verify_product_mirror,
 _creem.create_subscription_checkout, _creem.create_credits_checkout,
 _creem.creem_enabled, _creem.credits_ready,
 _creem.CREEM_CREDITS_CURRENCY) = _orig_creem
_creem._patch = _orig_patch
_creem._fetch_product = _orig_fetch_prod
_creem.CREEM_API_KEY = _orig_creem_key
_rec_fwd.fetch_subscription, _rec_fwd.fetch_transaction = _orig_fetch_sub, _orig_fetch_txn

# ═══ cleanup ═══
# op 驱动过 /samelabs plan 写入 → operator_audit 留有 actor 引用;
# FK 无 CASCADE, 测试清理先删 audit 行再删 actor users (test-only)。
with db() as con:
    con.execute(
        "delete from operator_audit where actor_user_id = ANY(%s)",
        ([u for u in (buyer, op, member, member2, member3, member4,
                      mmA, mmB, mmC, mmD) if u],))
    for uid in (buyer, op, member, member2, member3, member4,
                mmA, mmB, mmC, mmD):
        purge_users(con, [uid])
    con.execute("delete from plans where id in (%s, %s)", (plan_id, plan2_id))
    con.execute("delete from plans where id in (%s, %s)", (planA, planB))
    con.execute("delete from packages where id=%s", (pkg_id,))


finish()
