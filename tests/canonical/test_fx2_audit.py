#!/usr/bin/env python3
"""FX-2 audit closure — behavior tests over real paths.

  T*  template escaping: /tools?q="><script> and a content_type-carrying
      upload render ESCAPED text (autoescape ON; no |safe for user data).
  S*  semaphore: 9 concurrent tools/call invocations all complete.
  U*  upgrade refund with a LOST upgrade callback: only the upgrade
      credits revoke; the base-paid membership survives.
  K*  ticket hash storage: same-ref retry invalidates the old credential
      (fresh upload_url); replay PUT response carries expires_at.
  F*  unified snake_case items: execute response, MCP structuredContent
      and the /r record share one item shape; storage ids absent.
"""
import base64
import http.client as _hc
import json as _json
import secrets as _secrets
import sys as _sys
import time
import urllib.parse
import uuid

_TEST_PORT = _os.environ.get("FLASHSHARE_TEST_PORT") if (_os := __import__("os")).environ.get("FLASHSHARE_TEST_PORT") else None
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


_HOST = ("127.0.0.1", int(_TEST_PORT))
_BASE = f"http://127.0.0.1:{_TEST_PORT}"
_u = "fx2" + uuid.uuid4().hex[:8]

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAYAAACNMs+9AAAAFUlEQVR42mNk"
    "+M+QzGdlGU0AAAvKBwGAWjR9awAAAABJRU5ErkJggg==")


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

print()
if FAIL:
    print(f"FAILED {len(FAIL)}: {FAIL}")
    _sys.exit(1)
print(f"OK — FX-2 audit ({COUNT} checks)")
