"""Public /pricing shop.

Signed-out visitors see the free plan, every sellable membership plan and
credit pack, and the refund policy; buying opens the login modal. The
account credits page keeps balance, membership status and history and
links to /pricing for purchases. The refund policy is part of the terms
in every language.
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
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:140]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import json as _json
import time as _time

ts = int(_time.time())
_u = "pr" + _os.urandom(4).hex()

from database import db

_USERS, _PLANS, _PKGS = [], [], []


def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
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
             ts, ts)).fetchone()
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
            " TRUE, %s, %s) returning id", (f"pk_{_u}", ts, ts)).fetchone()
        _PKGS.append(int(k["id"]))
    plus_id = _mkplan(f"plus_{_u}", 999, 100, _so)
    pro_id = _mkplan(f"pro_{_u}", 2999, 500, _so + 1)
    off_id = _mkplan(f"off_{_u}", 1999, 300, _so + 2, active=False)
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
    uid = _mkuser(f"{_u}m")
    with db() as con:
        con.execute(
            "insert into memberships (user_id, plan_id, provider_subscription_id,"
            " status, started_at, renews_at, updated_at) values"
            " (%s, %s, %s, 'active', %s, %s, %s)",
            (uid, plus_id, f"sub_{_u}", ts, ts + 30 * 86400, ts))
    ck = create_browser_session(uid)
    st, html = _get("/pricing?lang=en", ck)
    check("P4 member: own plan marked current, higher plan offers upgrade",
          st == 200 and "pr-card on" in html
          and f'class="btn btn-p sub-upgrade" data-plan="pro_{_u}"' in html
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

print()
print(f"{COUNT} checks")
print("FAIL: " + (", ".join(FAIL) if FAIL else "none"))
_sys.exit(1 if FAIL else 0)
