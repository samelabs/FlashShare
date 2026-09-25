#!/usr/bin/env python3
"""Economy / membership closure canonical.

Proves over real routes/functions (Creem isolated by module fakes, DB
isolated, every row this suite creates is removed at the end):
  M1  re-subscribe after lapse: subscribe → checkout.completed →
      subscription.paid on a NEW subscription rebinds a dead Membership
      (expired / lapsed past renews_at); credits granted exactly once;
      a live Membership bound to another subscription still fails closed.
  M2  a pending downgrade is cleared when subscription.update confirms
      the current plan; the next renewal then succeeds.
  M3  refunding ONLY the upgrade (proration) charge reverses exactly the
      upgrade top-up and keeps the Membership; refunding the base period
      payment still ends the current period's paid validity.
  M4  platform income writer serializes on the platform users row:
      concurrent settlements get distinct consecutive balance_after.
  M5  a non-InsufficientCredits consume failure resolves the claim
      (no charge → failed; committed charge → exact refund → failed;
      unresolvable → running for startup recovery); retry is not stuck.
  M6  provider call log API: explicit {at, status, duration_s,
      cost_snapshot} shape (no failure text); drawer JS renders local
      time + i18n status keys present in all locales.
  M7  upgrade-credit documentation matches code (paid-only).
  M8  an already-applied period's paid replay never regresses
      canceled / past_due / newer period boundary.
  M9  authenticated access runs the idempotent reconcile for a lapsed
      Membership, throttled per user, in the background.
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

FAIL = []
COUNT = 0
def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:140]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import asyncio
import datetime
import hashlib as _hashlib
import hmac as _hmac
import json as _json
import threading
import time as _time

ts = int(_time.time())
_u = "mc" + _os.urandom(4).hex()

from database import db

_USERS = []
_PLANS = []
_FIRST_PARTY_TOOLS = []
_PLATFORM_IDEMS = []

def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    _USERS.append(int(row["id"]))
    return int(row["id"])

def _iso(v):
    return datetime.datetime.fromtimestamp(v, datetime.timezone.utc).isoformat()

def _cleanup():
    with db() as con:
        from governance.platform import ensure_platform_account
        pid = ensure_platform_account(con)
        for idem in _PLATFORM_IDEMS:
            con.execute("delete from ledger where user_id=%s and idem_key=%s",
                        (pid, idem))
        for tk in _FIRST_PARTY_TOOLS:
            con.execute("delete from pricing where tool_id=%s", (tk,))
            con.execute("delete from platform_tools where tool_key=%s", (tk,))
        for uid in _USERS:
            # uploads/ledger/orders/memberships/pending_downgrades/
            # tool_calls/sessions cascade with the users row
            con.execute("delete from pending_downgrades where user_id=%s", (uid,))
            con.execute("delete from memberships where user_id=%s", (uid,))
            con.execute("delete from users where id=%s", (uid,))
        for pid_ in _PLANS:
            con.execute("delete from plans where id=%s", (pid_,))
    from governance import plan_catalog
    plan_catalog.catalog(refresh=True)

try:
    # ═══ Creem isolation (module fakes; no network) ═══
    from economy import creem as _creem
    from economy import reconcile as _rec
    _creem.CREEM_WEBHOOK_SECRET = "mc-test-secret"
    _creem.creem_enabled = lambda: True
    def _fake_sub_checkout(order_id, plan, user):
        return {"ok": True, "checkout_url": f"https://creem.test/s/{order_id}",
                "checkout_id": f"ch_{_u}_{order_id}"}
    _creem.create_subscription_checkout = _fake_sub_checkout

    _TXNS = {}
    _rec.fetch_transaction = lambda txn_id: (dict(_TXNS[txn_id])
                                             if txn_id in _TXNS else None)

    def _signed(event):
        raw = _json.dumps(event).encode()
        sig = _hmac.new(b"mc-test-secret", raw, _hashlib.sha256).hexdigest()
        return raw, sig

    from fastapi.testclient import TestClient
    from app.main import app as _app
    from app.web_auth import create_browser_session, COOKIE_NAME
    client = TestClient(_app)

    def _hook(event_type, obj, evt_id=None):
        event = {"id": evt_id or f"evt_{_os.urandom(6).hex()}",
                 "eventType": event_type, "object": obj}
        raw, sig = _signed(event)
        r = client.post("/webhooks/creem", content=raw,
                        headers={"creem-signature": sig,
                                 "Content-Type": "application/json"})
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {}

    def _sub(sub_id, ps, pe, product, txn=None, status="active"):
        s = {"object": "subscription", "id": sub_id, "status": status,
             "current_period_start_date": _iso(ps),
             "current_period_end_date": _iso(pe),
             "product": {"id": product}}
        if txn:
            s["last_transaction_id"] = txn
        return s

    def _completed(order_id, sub_id, amount, product, checkout_id=None):
        return {"id": checkout_id or f"ch_{_u}_{order_id}",
                "object": "checkout",
                "request_id": f"order:{order_id}",
                "subscription": {"id": sub_id},
                "order": {"id": f"ord_{sub_id}", "status": "paid",
                          "type": "recurring", "amount": amount,
                          "currency": "USD", "product": product}}

    # ═══ plans: PLUS (100 credits) < PRO (500 credits) ═══
    from governance import plan_catalog
    def _mkplan(code, product, price, monthly, so):
        with db() as con:
            row = con.execute(
                "insert into plans (code, name, monthly_credits, price_cents,"
                " currency, creem_product_id, sort_order, active,"
                " ingress_files_24h, ingress_bytes_24h, expiry_days,"
                " download_limits, created_at, updated_at) values"
                " (%s, %s, %s, %s, 'USD', %s, %s, TRUE, 500, 524288000,"
                " ARRAY[7], ARRAY[50], %s, %s) returning id",
                (code, code.upper(), monthly, price, product, so, ts, ts)
            ).fetchone()
        _PLANS.append(int(row["id"]))
        return int(row["id"])
    with db() as con:
        _so = int(con.execute(
            "select coalesce(max(sort_order), 0) + 1 s from plans").fetchone()["s"])
    P_PLUS = f"prod_{_u}_plus"
    P_PRO = f"prod_{_u}_pro"
    plus_id = _mkplan(f"plus_{_u}", P_PLUS, 999, 100, _so)
    pro_id = _mkplan(f"pro_{_u}", P_PRO, 2999, 500, _so + 1)
    plan_catalog.catalog(refresh=True)

    from economy.billing import spendable_balance, grant as _grant
    from economy.subscription import handle_event as _hev

    def _membership(uid):
        with db() as con:
            r = con.execute(
                "select m.*, p.code from memberships m join plans p"
                " on p.id=m.plan_id where m.user_id=%s", (uid,)).fetchone()
        return dict(r) if r else None

    def _pending(uid):
        with db() as con:
            r = con.execute("select * from pending_downgrades where user_id=%s",
                            (uid,)).fetchone()
        return dict(r) if r else None

    def _subscribe(cookie, plan_id):
        r = client.post("/api/topup/subscribe", json={"plan_id": plan_id},
                        cookies={COOKIE_NAME: cookie})
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {}

    def _open_plan_order(uid):
        with db() as con:
            r = con.execute(
                "select * from orders where user_id=%s and kind='plan'"
                " and status='pending' order by id desc limit 1",
                (uid,)).fetchone()
        return dict(r) if r else None

    PS1, PE1 = ts - 5 * 86400, ts + 25 * 86400

    # ══════════ M1. re-subscribe after lapse ══════════
    def _first_subscription(uid, cookie, plan_id, product, price, sub_id,
                            txn, ps=PS1, pe=PE1):
        sc, d = _subscribe(cookie, plan_id)
        o = _open_plan_order(uid)
        sc2, d2 = _hook("checkout.completed",
                        _completed(o["id"], sub_id, price, product))
        sc3, d3 = _hook("subscription.paid", _sub(sub_id, ps, pe, product, txn))
        return (sc, d), (sc2, d2), (sc3, d3), o

    for variant in ("expired", "lapsed"):
        ua = _mkuser(f"{_u}r{variant[:2]}")
        ck = create_browser_session(ua)
        sa, sb = f"sub_{_u}_{variant}_A", f"sub_{_u}_{variant}_B"
        (s1, d1), (s2, d2), (s3, d3), oa = _first_subscription(
            ua, ck, plus_id, P_PLUS, 999, sa, f"txn_{_u}_{variant}_A1")
        m = _membership(ua)
        check(f"M1.{variant}.0 first subscription live (plus, +100)",
              s1 == 200 and s2 == 200 and s3 == 200 and m
              and m["status"] == "active" and m["code"] == f"plus_{_u}"
              and spendable_balance(ua) == 100,
              f"{s1}/{d1} {s2}/{d2} {s3}/{d3} {m} bal={spendable_balance(ua)}")
        # a still-live membership blocks a second checkout
        sc, d = _subscribe(ck, pro_id)
        check(f"M1.{variant}.1 live membership → subscribe 409 plan_change_required",
              sc == 409 and d.get("error") == "plan_change_required", f"{sc} {d}")
        # leave a stale scheduled downgrade on the old subscription
        with db() as con:
            con.execute(
                "insert into pending_downgrades (user_id, target_plan_id,"
                " effective_at) values (%s, %s, %s)", (ua, plus_id, PE1))
        if variant == "expired":
            sc, d = _hook("subscription.expired",
                          _sub(sa, PS1, ts - 60, P_PLUS, status="expired"))
            ok_lapse = sc == 200 and _membership(ua)["status"] == "expired"
        else:
            # canceled, and the paid-through date has passed (no event)
            sc, d = _hook("subscription.canceled",
                          _sub(sa, PS1, PE1, P_PLUS, status="canceled"))
            with db() as con:
                con.execute("update memberships set renews_at=%s"
                            " where user_id=%s", (ts - 60, ua))
            m = _membership(ua)
            ok_lapse = sc == 200 and m["status"] == "canceled" \
                and int(m["renews_at"]) < ts
        check(f"M1.{variant}.2 old subscription {variant}", ok_lapse, f"{sc} {d}")
        bal0 = spendable_balance(ua)
        sc, d = _subscribe(ck, pro_id)
        ob = _open_plan_order(ua)
        check(f"M1.{variant}.3 dead membership → new checkout allowed",
              sc == 200 and d.get("checkout_url") and ob
              and int(ob["item_id"]) == pro_id, f"{sc} {d}")
        sc, d = _hook("checkout.completed",
                      _completed(ob["id"], sb, 2999, P_PRO))
        m = _membership(ua)
        with db() as con:
            obr = dict(con.execute("select * from orders where id=%s",
                                   (ob["id"],)).fetchone())
        check(f"M1.{variant}.4 checkout.completed rebinds the dead slot (200,"
              " order paid sub:B, membership sub:B pro, no credits yet)",
              sc == 200 and obr["status"] == "paid"
              and obr["provider_order_id"] == f"sub:{sb}"
              and m["provider_subscription_id"] == f"sub:{sb}"
              and m["code"] == f"pro_{_u}" and m["status"] == "active"
              and m["renews_at"] is None
              and spendable_balance(ua) == bal0,
              f"{sc} {d} {obr.get('status')} {m}")
        check(f"M1.{variant}.5 stale pending downgrade of the dead sub cleared",
              _pending(ua) is None, str(_pending(ua)))
        PS2, PE2 = ts - 60, ts + 30 * 86400
        sc, d = _hook("subscription.paid",
                      _sub(sb, PS2, PE2, P_PRO, f"txn_{_u}_{variant}_B1"))
        m = _membership(ua)
        check(f"M1.{variant}.6 subscription.paid → active pro, +500",
              sc == 200 and m["status"] == "active" and m["code"] == f"pro_{_u}"
              and int(m["renews_at"]) == PE2
              and spendable_balance(ua) == bal0 + 500,
              f"{sc} {d} {m} bal={spendable_balance(ua)}")
        sc, d = _hook("subscription.paid",
                      _sub(sb, PS2, PE2, P_PRO, f"txn_{_u}_{variant}_B1"))
        sc_c, d_c = _hook("checkout.completed",
                          _completed(ob["id"], sb, 2999, P_PRO))
        check(f"M1.{variant}.7 paid + checkout replays → credits exactly once",
              sc == 200 and sc_c == 200 and spendable_balance(ua) == bal0 + 500,
              f"{sc} {d} {sc_c} {d_c} bal={spendable_balance(ua)}")
        # replay of the OLD subscription's checkout never moves the slot back
        sc, d = _hook("checkout.completed",
                      _completed(oa["id"], sa, 999, P_PLUS))
        m = _membership(ua)
        check(f"M1.{variant}.8 old checkout replay → 500 membership_occupied,"
              " slot stays on sub:B",
              sc == 500 and d.get("error") == "membership_occupied"
              and m["provider_subscription_id"] == f"sub:{sb}",
              f"{sc} {d} {m}")

    # negative: live membership bound to a different subscription
    un = _mkuser(f"{_u}neg")
    ckn = create_browser_session(un)
    sc_, sn = f"sub_{_u}_negC", f"sub_{_u}_negD"
    _first_subscription(un, ckn, plus_id, P_PLUS, 999, sc_, f"txn_{_u}_negC1")
    from repo import orders as orders_repo
    with db() as con:
        od = orders_repo.insert_order(con, un, "plan", pro_id, "creem", 2999,
                                      "USD", ts)
    bal_n = spendable_balance(un)
    sc, d = _hook("checkout.completed", _completed(od["id"], sn, 2999, P_PRO))
    m = _membership(un)
    with db() as con:
        odr = dict(con.execute("select status, provider_order_id from orders"
                               " where id=%s", (od["id"],)).fetchone())
    check("M1.neg live membership on another sub → 500 membership_occupied,"
          " zero mutation",
          sc == 500 and d.get("error") == "membership_occupied"
          and m["provider_subscription_id"] == f"sub:{sc_}"
          and m["code"] == f"plus_{_u}" and m["status"] == "active"
          and odr["status"] == "pending" and odr["provider_order_id"] is None
          and spendable_balance(un) == bal_n,
          f"{sc} {d} {m} {odr}")
    # bound-but-unpaid (active + renews None) is occupied too
    with db() as con:
        con.execute("update memberships set renews_at=NULL where user_id=%s",
                    (un,))
    sc, d = _hook("checkout.completed", _completed(od["id"], sn, 2999, P_PRO))
    check("M1.neg2 bound-but-unpaid membership → still membership_occupied",
          sc == 500 and d.get("error") == "membership_occupied"
          and _membership(un)["provider_subscription_id"] == f"sub:{sc_}",
          f"{sc} {d}")

    # ══════════ M2. pending downgrade cleared by current-plan update ══════════
    def _seed_member(tag, plan_id, product, sub_id, txn, ps=PS1, pe=PE1):
        uid = _mkuser(f"{_u}{tag}")
        with db() as con:
            con.execute(
                "insert into orders (user_id, kind, item_id, provider,"
                " provider_order_id, status, amount_cents, currency, paid_at,"
                " created_at) values (%s, 'plan', %s, 'creem', %s, 'paid',"
                " 999, 'USD', %s, %s)", (uid, plan_id, "sub:" + sub_id, ts, ts))
            con.execute(
                "insert into memberships (user_id, plan_id,"
                " provider_subscription_id, status, started_at, renews_at,"
                " updated_at) values (%s, %s, %s, 'active', %s, NULL, %s)",
                (uid, plan_id, "sub:" + sub_id, ts, ts))
        _hev("subscription.paid", _sub(sub_id, ps, pe, product, txn))
        return uid

    s2 = f"sub_{_u}_dg"
    u2 = _seed_member("dg", pro_id, P_PRO, s2, f"txn_{_u}_dg1")
    r = _hev("subscription.update", _sub(s2, PS1, PE1, P_PLUS))
    p = _pending(u2)
    check("M2.1 downgrade update → pending downgrade to plus, membership stays pro",
          r.get("deferred") == "pending_downgrade" and p
          and int(p["target_plan_id"]) == plus_id
          and _membership(u2)["code"] == f"pro_{_u}", f"{r} {p}")
    r = _hev("subscription.update", _sub(s2, PS1, PE1, P_PRO))
    check("M2.2 update confirming current plan → pending downgrade cleared",
          r.get("pending_downgrade_cleared") is True and _pending(u2) is None
          and _membership(u2)["code"] == f"pro_{_u}", str(r))
    r = _hev("subscription.update", _sub(s2, PS1, PE1, P_PRO))
    check("M2.3 replay is idempotent (no pending, no error)",
          r.get("updated") is True and "pending_downgrade_cleared" not in r
          and _pending(u2) is None, str(r))
    b2 = spendable_balance(u2)
    PSn, PEn = PE1, PE1 + 30 * 86400
    try:
        r = _hev("subscription.paid", _sub(s2, PSn, PEn, P_PRO, f"txn_{_u}_dg2"))
        err = ""
    except KeyError as exc:
        err = str(exc)
    m = _membership(u2)
    check("M2.4 renewal on current plan succeeds (+500 pro, renews advanced)",
          not err and m["code"] == f"pro_{_u}" and int(m["renews_at"]) == PEn
          and spendable_balance(u2) == b2 + 500,
          f"{err} {m} bal={spendable_balance(u2)}")

    # ══════════ M3. partial upgrade refund ══════════
    def _upgrade(uid, sub_id, txn_up):
        _hev("subscription.update", _sub(sub_id, PS1, PE1, P_PRO))
        _hev("subscription.paid", _sub(sub_id, PS1, PE1, P_PRO, txn_up))

    def _refund_evt(txn_id, sub_id, ps=PS1, pe=PE1):
        _TXNS[txn_id] = {"object": "transaction", "id": txn_id,
                         "status": "refunded", "type": "invoice",
                         "subscription": sub_id,
                         "period_start": _iso(ps), "period_end": _iso(pe)}
        return _hook("refund.created", {"id": f"ref_{txn_id}",
                                        "object": "refund",
                                        "transaction": {"id": txn_id}})

    s3 = f"sub_{_u}_up"
    u3 = _seed_member("up", plus_id, P_PLUS, s3, f"txn_{_u}_up_base")
    _upgrade(u3, s3, f"txn_{_u}_up_charge")
    m0 = _membership(u3)
    check("M3.0 base +100 then upgrade top-up +400 (pro)",
          spendable_balance(u3) == 500 and m0["code"] == f"pro_{_u}",
          f"bal={spendable_balance(u3)} {m0}")
    sc, d = _refund_evt(f"txn_{_u}_up_charge", s3)
    m = _membership(u3)
    check("M3.1 upgrade-charge refund → exactly -400",
          sc == 200 and d.get("credits") == -400 and spendable_balance(u3) == 100,
          f"{sc} {d} bal={spendable_balance(u3)}")
    check("M3.2 upgrade-charge refund does NOT expire the membership",
          m["status"] == "active" and int(m["renews_at"]) == PE1
          and int(m["last_grant_period_start"]) == PS1, str(m))
    sc, d = _refund_evt(f"txn_{_u}_up_charge", s3)
    check("M3.3 upgrade refund replay → no second revoke, still active",
          sc == 200 and spendable_balance(u3) == 100
          and _membership(u3)["status"] == "active",
          f"{sc} {d} bal={spendable_balance(u3)}")
    sc, d = _refund_evt(f"txn_{_u}_up_base", s3)
    m = _membership(u3)
    check("M3.4 base-period refund → -100 and current period expired (unchanged)",
          sc == 200 and d.get("credits") == -100 and spendable_balance(u3) == 0
          and m["status"] == "expired" and int(m["renews_at"]) <= int(_time.time()) + 2,
          f"{sc} {d} {m}")
    s3b = f"sub_{_u}_upb"
    u3b = _seed_member("upb", plus_id, P_PLUS, s3b, f"txn_{_u}_upb_base")
    _upgrade(u3b, s3b, f"txn_{_u}_upb_charge")
    sc, d = _refund_evt(f"txn_{_u}_upb_base", s3b)
    m = _membership(u3b)
    check("M3.5 base refund with an un-refunded upgrade → -100 only, expired",
          sc == 200 and d.get("credits") == -100
          and spendable_balance(u3b) == 400 and m["status"] == "expired",
          f"{sc} {d} {m} bal={spendable_balance(u3b)}")

    # ══════════ M4. platform income serialization ══════════
    from governance.platform import record_platform_income, ensure_platform_account
    _pid = ensure_platform_account()
    _ids = [f"{_u}m4a", f"{_u}m4b"]
    _amt = {_ids[0]: 7, _ids[1]: 11}
    for cid in _ids:
        _PLATFORM_IDEMS.append(f"platform_revenue:{cid}")
    _barrier = threading.Barrier(2)
    _m4err = []
    def _settle(cid):
        try:
            _barrier.wait(timeout=10)
            with db() as con:
                record_platform_income(con, cid, "platform_revenue", _amt[cid],
                                       "m4 concurrency", ts)
                _time.sleep(0.5)   # hold the settlement tx open
        except Exception as exc:
            _m4err.append(repr(exc))
    _th = [threading.Thread(target=_settle, args=(c,)) for c in _ids]
    for t in _th:
        t.start()
    for t in _th:
        t.join(timeout=30)
    with db() as con:
        rows = [dict(r) for r in con.execute(
            "select id, ref_id, delta, balance_after from ledger"
            " where user_id=%s and idem_key = any(%s) order by id",
            (_pid, [f"platform_revenue:{c}" for c in _ids])).fetchall()]
        total = int(con.execute(
            "select coalesce(sum(delta),0) s from ledger where user_id=%s"
            " and kind in ('platform_commission','platform_revenue')",
            (_pid,)).fetchone()["s"])
    ok4 = (not _m4err and len(rows) == 2
           and rows[1]["balance_after"] == rows[0]["balance_after"] + rows[1]["delta"]
           and rows[1]["balance_after"] == total)
    check("M4 two parallel settlements → distinct consecutive balance_after",
          ok4, f"{_m4err} {rows} total={total}")

    # ══════════ M5. consume failure resolves the claim ══════════
    from execution import gate as _gate
    from execution.invocation import ToolInvocation, SourceFile
    from execution.results import StagedItems, StagedFileItem
    from repo.ledger import by_idem_key
    tool5 = f"mctool_{_u}"
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, tool_type, label_json, icon, visibility, enabled,"
            " created_at, updated_at)"
            " values (%s, NULL, %s, 'local', 'produce', %s, 'wand',"
            " 'public', TRUE, %s, %s)",
            (tool5, tool5, _json.dumps({"en": "MC"}), ts, ts))
        con.execute("insert into pricing (tool_id, cost, enabled, updated_at)"
                    " values (%s, 25, TRUE, %s)", (tool5, ts))
    _FIRST_PARTY_TOOLS.append(tool5)
    u5 = _mkuser(f"{_u}c5")
    _grant(u5, 1000, "manual_topup", note="mc")

    def _inv(ref):
        return ToolInvocation(tool_id=tool5, files=[SourceFile("p", "t")],
                              params={}, delivery={"expiry_days": 7,
                                                   "allowed_downloads": 5},
                              client_ref=ref)
    _stage_n = [0]
    async def _stage_ok():
        _stage_n[0] += 1
        return StagedItems(
            order=["file"], inline=[],
            files=[StagedFileItem(
                gokapi_id=f"gk_{_u}_{_stage_n[0]}", filename="o.bin",
                content_type="application/octet-stream", size=10,
                expiry_days=7, allowed_downloads=5,
                expire_at=ts + 86400, url_hotlink="")],
            ip="127.0.0.1")
    def _run(ref):
        return asyncio.new_event_loop().run_until_complete(_gate.run(
            {"id": u5}, {"id": tool5, "tool_key": tool5, "type": "produce",
                         "owner_id": None}, _inv(ref), _stage_ok))
    def _calls(ref):
        with db() as con:
            return [dict(r) for r in con.execute(
                "select id, status, error from tool_calls where user_id=%s"
                " and client_ref=%s order by id", (u5, ref)).fetchall()]

    _real_consume = _gate.consume
    # a) consume fails WITHOUT committing
    def _consume_boom(*a, **kw):
        raise RuntimeError("db connection lost")
    _gate.consume = _consume_boom
    try:
        ref_a = f"m5a:{_u}"
        b5 = spendable_balance(u5)
        res, err, st = _run(ref_a)
    finally:
        _gate.consume = _real_consume
    ca = _calls(ref_a)
    with db() as con:
        chg_a = by_idem_key(con, u5, f"call:{ca[0]['id']}") if ca else None
    check("M5a consume error (not committed) → 500 err_metering_failed, call"
          " failed, zero charge",
          st == 500 and err.get("error") == "err_metering_failed"
          and len(ca) == 1 and ca[0]["status"] == "failed"
          and chg_a is None and spendable_balance(u5) == b5,
          f"{st} {err} {ca} bal={spendable_balance(u5)}")
    res, err, st = _run(ref_a)
    ca = _calls(ref_a)
    check("M5a2 retry with the same client_ref is not stuck (runs, charged once)",
          res and res.get("ok") and err is None
          and [c["status"] for c in ca] == ["failed", "ok"]
          and spendable_balance(u5) == b5 - 25,
          f"{res} {err} {ca} bal={spendable_balance(u5)}")
    # b) consume COMMITS, then the call raises (ACK loss)
    def _consume_commit_then_boom(*a, **kw):
        _real_consume(*a, **kw)
        raise RuntimeError("ack lost")
    _gate.consume = _consume_commit_then_boom
    try:
        ref_b = f"m5b:{_u}"
        b5b = spendable_balance(u5)
        res, err, st = _run(ref_b)
    finally:
        _gate.consume = _real_consume
    cb = _calls(ref_b)
    with db() as con:
        chg = by_idem_key(con, u5, f"call:{cb[0]['id']}") if cb else None
        rf = by_idem_key(con, u5, f"refund:ledger:{chg['id']}") if chg else None
    check("M5b committed charge + exception → exact refund, call failed",
          st == 500 and err.get("error") == "err_metering_failed"
          and len(cb) == 1 and cb[0]["status"] == "failed"
          and chg is not None and rf is not None
          and int(rf["delta"]) == -int(chg["delta"])
          and spendable_balance(u5) == b5b,
          f"{st} {err} {cb} chg={chg and chg['delta']} rf={rf and rf['delta']}")
    # c) resolution cannot finalize → stays running → startup recovery
    _real_ffail = _gate.finalize_call_failed
    def _ffail_boom(*a, **kw):
        raise RuntimeError("db down")
    _gate.consume = _consume_boom
    _gate.finalize_call_failed = _ffail_boom
    try:
        ref_c = f"m5c:{_u}"
        res, err, st = _run(ref_c)
    finally:
        _gate.consume = _real_consume
        _gate.finalize_call_failed = _real_ffail
    cc = _calls(ref_c)
    rec = _gate.recover_abandoned_calls()
    cc2 = _calls(ref_c)
    check("M5c unresolvable → stays running (500), startup recovery settles it",
          st == 500 and cc and cc[0]["status"] == "running"
          and cc2[0]["status"] == "failed", f"{st} {cc} {cc2} {rec}")

    # ══════════ M6. provider call log API + drawer ══════════
    owner = _mkuser(f"{_u}ow")
    caller = _mkuser(f"{_u}ca")
    with db() as con:
        oname = con.execute("select username from users where id=%s",
                            (owner,)).fetchone()["username"]
    tid6 = f"t6{_u}"
    tkey6 = f"{oname}/{tid6}"
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, tool_type, label_json, icon,"
            " visibility, enabled, endpoint_json, created_at, updated_at)"
            " values (%s, %s, %s, 'http', 'produce', %s, 'wand',"
            " 'unlisted', TRUE, %s, %s, %s)",
            (tkey6, owner, tid6, _json.dumps({"en": "T6"}),
             _json.dumps({"url": "https://example.com/x", "timeout_s": 10}),
             ts, ts))
        h = "a" * 64
        con.execute(
            "insert into tool_calls (client_ref, user_id, tool_id, status,"
            " cost_snapshot, error, started_at, finished_at,"
            " commission_rate_snapshot, invocation_hash) values"
            " ('r1', %s, %s, 'failed', 25, 'balance:12345', %s, %s, 20, %s),"
            " ('r2', %s, %s, 'running', 25, NULL, %s, NULL, 20, %s)",
            (caller, tkey6, ts - 100, ts - 97, h, caller, tkey6, ts - 50, h))
    ck6 = create_browser_session(owner)
    r = client.get(f"/api/usertools/{tid6}/calls", cookies={COOKIE_NAME: ck6})
    d = r.json()
    calls6 = d.get("calls") or []
    shape_ok = all(set(c) == {"at", "status", "duration_s", "cost_snapshot"}
                   for c in calls6)
    by_st = {c["status"]: c for c in calls6}
    check("M6.1 call log shape {at,status,duration_s,cost_snapshot}, no failure text",
          r.status_code == 200 and len(calls6) == 2 and shape_ok
          and "balance:12345" not in r.text, f"{r.status_code} {d}")
    check("M6.2 at = started_at epoch seconds; duration_s = finished-started;"
          " running → None",
          by_st.get("failed", {}).get("at") == ts - 100
          and by_st["failed"]["duration_s"] == 3
          and by_st.get("running", {}).get("duration_s") is None
          and isinstance(by_st["running"]["at"], int), str(calls6))
    _js = open(_os.path.join(_REPO, "static", "pages", "tools.js"),
               encoding="utf-8").read()
    check("M6.3 drawer renders at in local time + i18n status (no created_at/ISO)",
          "created_at" not in _js and "toISOString" not in _js
          and "toLocaleString" in _js and "'mytools_call_st_' + c.status" in _js)
    from i18n import SERVER_I18N
    _keys = [f"mytools_call_st_{s}" for s in ("ok", "failed", "insufficient",
                                              "running")]
    _i18n_js = open(_os.path.join(_REPO, "static", "i18n.js"),
                    encoding="utf-8").read()
    check("M6.4 status keys present in all 10 locales (source + generated)",
          len(SERVER_I18N) == 10
          and all(all(SERVER_I18N[l].get(k) for k in _keys) for l in SERVER_I18N)
          and all(k in _i18n_js for k in _keys))

    # ══════════ M7. docs truth ══════════
    _topup = open(_os.path.join(_REPO, "app", "routes", "topup.py"),
                  encoding="utf-8").read()
    check("M7 topup comments: upgrade credits are paid-only (no update-event grant claim)",
          "由 update 事件补授" not in _topup and "由 update 事件补差" not in _topup)

    # ══════════ M8. paid replay never regresses lifecycle ══════════
    for st_evt, st_name in (("subscription.canceled", "canceled"),
                            ("subscription.past_due", "past_due")):
        s8 = f"sub_{_u}_{st_name}"
        u8 = _seed_member(st_name[:4] + "8", plus_id, P_PLUS, s8,
                          f"txn_{_u}_{st_name}1")
        _hev(st_evt, _sub(s8, PS1, PE1, P_PLUS, status=st_name))
        b8 = spendable_balance(u8)
        r8 = _hev("subscription.paid", _sub(s8, PS1, PE1, P_PLUS,
                                            f"txn_{_u}_{st_name}1"))
        m = _membership(u8)
        check(f"M8.{st_name} same-period paid replay keeps {st_name}, zero grant",
              m["status"] == st_name and int(m["renews_at"]) == PE1
              and spendable_balance(u8) == b8 and r8.get("credits") == 0,
              f"{r8} {m}")
    s8o = f"sub_{_u}_ooo"
    u8o = _seed_member("ooo8", plus_id, P_PLUS, s8o, f"txn_{_u}_ooo1")
    PSb, PEb = PE1, PE1 + 30 * 86400
    _hev("subscription.paid", _sub(s8o, PSb, PEb, P_PLUS, f"txn_{_u}_ooo2"))
    _hev("subscription.canceled", _sub(s8o, PSb, PEb, P_PLUS, status="canceled"))
    b8o = spendable_balance(u8o)
    _hev("subscription.paid", _sub(s8o, PS1, PE1, P_PLUS, f"txn_{_u}_ooo1"))
    m = _membership(u8o)
    check("M8.ooo late older-period paid → newer boundary + canceled kept, zero grant",
          m["status"] == "canceled" and int(m["renews_at"]) == PEb
          and int(m["last_grant_period_start"]) == PSb
          and spendable_balance(u8o) == b8o, str(m))
    s8n = f"sub_{_u}_new"
    u8n = _seed_member("new8", plus_id, P_PLUS, s8n, f"txn_{_u}_new1")
    _hev("subscription.past_due", _sub(s8n, PS1, PE1, P_PLUS, status="past_due"))
    _hev("subscription.paid", _sub(s8n, PSb, PEb, P_PLUS, f"txn_{_u}_new2"))
    m = _membership(u8n)
    check("M8.new a NEW period's paid still recovers past_due → active",
          m["status"] == "active" and int(m["renews_at"]) == PEb, str(m))

    # ══════════ M9. access-triggered reconcile (throttled) ══════════
    _fetched = []
    _SUBS = {}
    def _fake_fetch_sub(sub_id):
        _fetched.append(sub_id)
        s = _SUBS.get(sub_id)
        return dict(s) if s else None
    _rec.fetch_subscription = _fake_fetch_sub
    s9 = f"sub_{_u}_acc"
    u9 = _seed_member("acc9", plus_id, P_PLUS, s9, f"txn_{_u}_acc1",
                      ps=ts - 40 * 86400, pe=ts - 10 * 86400)
    with db() as con:
        con.execute("update memberships set status='active' where user_id=%s",
                    (u9,))
    PS9, PE9 = ts - 10 * 86400, ts + 20 * 86400
    _SUBS[s9] = _sub(s9, PS9, PE9, P_PLUS, f"txn_{_u}_acc2")
    _TXNS[f"txn_{_u}_acc2"] = {"object": "transaction", "id": f"txn_{_u}_acc2",
                               "status": "paid", "type": "invoice",
                               "subscription": s9, "period_start": _iso(PS9),
                               "period_end": _iso(PE9)}
    b9 = spendable_balance(u9)
    ck9 = create_browser_session(u9)
    def _wait(pred, sec=10):
        end = _time.time() + sec
        while _time.time() < end:
            if pred():
                return True
            _time.sleep(0.1)
        return pred()
    client.get("/healthz", cookies={COOKIE_NAME: ck9})
    ok9 = _wait(lambda: (_membership(u9) or {}).get("renews_at") == PE9
                and spendable_balance(u9) == b9 + 100)
    m = _membership(u9)
    check("M9.1 session access with lapsed renews_at → reconcile heals the"
          " lost renewal (+100, renews advanced)",
          ok9 and _fetched.count(s9) == 1 and m["status"] == "active",
          f"{m} fetched={_fetched} bal={spendable_balance(u9)}")
    with db() as con:
        con.execute("update memberships set renews_at=%s where user_id=%s",
                    (ts - 5, u9))
    client.get("/healthz", cookies={COOKIE_NAME: ck9})
    _time.sleep(1.0)
    check("M9.2 second access within the throttle window → no second attempt",
          _fetched.count(s9) == 1, str(_fetched))
    s9b = f"sub_{_u}_accb"
    u9b = _seed_member("acb9", plus_id, P_PLUS, s9b, f"txn_{_u}_accb1")
    client.get("/healthz", cookies={COOKIE_NAME: create_browser_session(u9b)})
    _time.sleep(1.0)
    check("M9.3 live (not lapsed) membership → no provider fetch",
          s9b not in _fetched, str(_fetched))
finally:
    _cleanup()

with db() as con:
    left = con.execute(
        "select (select count(*) from users where username like %s) u,"
        " (select count(*) from plans where code like %s) p,"
        " (select count(*) from platform_tools where tool_key like %s) t",
        (f"{_u}%", f"%{_u}%", f"%{_u}%")).fetchone()
check("CLEANUP all suite rows removed",
      int(left["u"]) == 0 and int(left["p"]) == 0 and int(left["t"]) == 0,
      str(dict(left)))

print()
print(f"FAIL: {' — '.join(FAIL) if FAIL else '无'} — ({COUNT} checks)")
_sys.exit(1 if FAIL else 0)
