#!/usr/bin/env python3
"""Owner verified badge canonical — batch membership predicate + projection.

  A*  effective_member_ids() is the batch read side of plan_of().member:
      identical verdicts for active / expired / no-membership users, one
      round trip, empty input → empty set (fail-closed toward "no badge").
  B*  Catalog projection: third-party tools carry owner_verified driven
      by that same predicate; first-party rows (owner NULL) always
      project False. Official tools never badge.
  C*  Badge disappearance: the moment the membership is no longer
      effective (renews_at in the past), the projection flips to False —
      same predicate, no separate membership logic anywhere.
"""
import os as _os
import sys as _sys
import time

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

ts = int(time.time())
_sfx = _os.urandom(3).hex()
from database import db
from economy.subscription import plan_of, effective_member_ids
from governance.catalog import list_catalog, search_catalog, market_page


def _new_user(prefix):
    with db() as con:
        con.execute(
            "insert into users (username, display_name, status, created_at,"
            " last_login_at, updated_at)"
            " values (%s, %s, 'active', %s, %s, %s) returning id",
            (prefix + _sfx, prefix + _sfx, ts, ts, ts))
        return con.execute("select id from users where username=%s",
                           (prefix + _sfx,)).fetchone()["id"]


def _seed_plan(user_id, status, renews_at):
    with db() as con:
        row = con.execute("select id from plans where code=%s", ("plus",)).fetchone()
        if not row:
            con.execute(
                "insert into plans (code, name, monthly_credits, price_cents, created_at, updated_at)"
                " values ('plus', 'PLUS', 0, 0, %s, %s) returning id", (ts, ts))
            plan_id = con.execute("select id from plans where code='plus'").fetchone()["id"]
        else:
            plan_id = row["id"]
        con.execute(
            "insert into memberships (user_id, plan_id, provider_subscription_id, status,"
            " started_at, renews_at, updated_at) values (%s, %s, %s, %s, %s, %s, %s)"
            " on conflict do nothing",
            (user_id, plan_id, f"sub:ov:{user_id}", status, ts, renews_at, ts))


# ═══ A. batch predicate ≡ plan_of().member (access projection verdict) ═══
u_active = _new_user("ovact")
u_expired = _new_user("ovexp")
u_free = _new_user("ovfree")
_seed_plan(u_active, "active", ts + 30 * 86400)
_seed_plan(u_expired, "active", ts - 100)

verdicts = {u: bool((plan_of(u) or {}).get("member"))
            for u in (u_active, u_expired, u_free)}
batch = effective_member_ids([u_active, u_expired, u_free])
check("OV-A1 active member in batch set", batch == {u_active},
      f"batch={batch} plan_of verdicts={verdicts}")
check("OV-A2 batch verdicts identical to plan_of().member",
      all(((u in batch) == verdicts[u]) for u in verdicts))
check("OV-A3 expired/absent memberships never badge",
      u_expired not in batch and u_free not in batch)
check("OV-A4 empty input → empty set (fail-closed)", effective_member_ids([]) == set())
check("OV-A5 duplicates collapse", effective_member_ids([u_active, u_active]) == {u_active})

# ═══ B/C. catalog projection owner_verified ═══
official = [t for t in list_catalog() if t["origin"] == "official"]
check("OV-B1 seeded catalog has first-party tools", len(official) >= 1, len(official))
check("OV-B2 official tool projects owner_verified=False",
      all(t["owner_verified"] is False for t in official))

KEY = official[0]["id"]
with db() as con:
    con.execute("update platform_tools set owner_id=%s where tool_key=%s",
                (u_active, KEY))
try:
    items = [t for t in search_catalog("") if t["id"] == KEY]
    check("OV-B3 verified owner surfaces on the market projection",
          len(items) == 1 and items[0]["owner_verified"] is True, items and items[0].get("owner_verified"))
    page = [t for t in market_page(q=KEY)["items"] if t["id"] == KEY]
    check("OV-B4 market_page projection carries the same flag",
          len(page) == 1 and page[0]["owner_verified"] is True)
    listed = [t for t in list_catalog() if t["id"] == KEY]
    check("OV-B5 list_catalog projection carries the same flag",
          len(listed) == 1 and listed[0]["owner_verified"] is True)

    # ═══ C. expiry flips the flag — no second membership judgment ═══
    with db() as con:
        con.execute("update memberships set renews_at=%s where user_id=%s",
                    (ts - 100, u_active))
    after = [t for t in search_catalog("") if t["id"] == KEY]
    check("OV-C1 expired membership → projection False (badge disappears)",
          len(after) == 1 and after[0]["owner_verified"] is False)
    check("OV-C2 plan_of agrees with the projection",
          (plan_of(u_active) or {}).get("member") is False)
finally:
    with db() as con:
        con.execute("update platform_tools set owner_id=NULL where tool_key=%s", (KEY,))

with db() as con:
    purge_users(con, [u_active, u_expired, u_free])
print(f"\n{COUNT - len(FAIL)}/{COUNT} passed")
if FAIL:
    print("FAIL:", *FAIL, sep="\n  ")
    _sys.exit(1)
print("ALL GREEN")
