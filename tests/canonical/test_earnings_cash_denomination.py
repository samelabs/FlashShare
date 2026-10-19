#!/usr/bin/env python3
"""S3 canonical — provider earnings cash denomination (USD-millis).

Two asset worlds, one settlement boundary:
  Consumer Spendable Credits (consume/refund/grant) — unchanged.
  Provider/platform cash in integer USD-millis via economy.money.

Checks (real DB, fresh rows):
  1. money authority: credits→millis, exact USD strings, decimal parse,
     sub-cent rejection (unit-level)
  2. consumer debit on a paid call is still Credits (spendable pool)
  3. third-party paid call: provider_earn + platform_commission both
     USD-millis (== credit share at 1:1), split math unchanged
  4. first-party paid call: platform_revenue = USD-millis
  5. provider summary / per-tool / revenue lines all USD (exact strings
     from the authority), no Credit fallback
  6. withdrawal minimum = $10.00; '10.25' → 10250 exactly;
     '10.001' → reject, zero rounding
  7. full user path: request → pending → approve → ledger withdraw in
     USD-millis; earnings=credits semantics gone (request_reject check)
  8. S2 attribution still green (per-tool rows relation-derived)
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
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:110]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import time as _time
ts = int(_time.time())
_u = "s3m" + _os.urandom(4).hex()

from database import db
from economy import earnings
from economy.money import (credits_to_millis, millis_to_usd_str,
                           millis_to_usd_cents_str, parse_usd_decimal)

# ── 1. money authority unit behavior ──
check("1a 1000 Credits = $1.00 → 1000 millis",
      credits_to_millis(1000) == 1000 and credits_to_millis(500) == 500)
check("1b exact USD strings",
      millis_to_usd_str(10250) == "$10.25" and millis_to_usd_str(10000) == "$10"
      and millis_to_usd_str(25500) == "$25.50")
check("1c '10.25' → 10250 exactly", parse_usd_decimal("10.25") == 10250)
check("1c2 '10.2500' (trailing zeros) → 10250",
      parse_usd_decimal("10.2500") == 10250)
try:
    parse_usd_decimal("10.001"); check("1d sub-cent rejected", False, "no raise")
except ValueError as e:
    check("1d sub-cent rejected → sub_cent_amount", str(e) == "sub_cent_amount", str(e))
_non_str_ok = True
_non_str_err = ""
for bad in (10.25, 10, None, True, [10.25], {"v": 1}):
    try:
        parse_usd_decimal(bad)
        _non_str_ok = False; _non_str_err = repr(bad); break
    except ValueError as e:
        if str(e) != "invalid_usd_amount":
            _non_str_ok = False; _non_str_err = f"{bad!r}→{e}"; break
check("1e non-str inputs (number/None/bool/list/dict) → invalid_usd_amount",
      _non_str_ok, _non_str_err)
try:
    parse_usd_decimal("abc.12"); check("1f malformed → invalid_usd_amount", False, "no raise")
except ValueError as e:
    check("1f malformed → invalid_usd_amount", str(e) == "invalid_usd_amount", str(e))

# S3 amend-2: money authority forbids internal coercion — real ints only,
# bool is not a money integer, float/str/None fail closed everywhere.
from economy.money import strict_int as _si
_type_ok, _type_err = True, ""
for fn, args in ((credits_to_millis, (123,)), (millis_to_usd_str, (123,)),):
    for bad in (10.25, True, False, "100", None):
        try:
            fn(*( [bad] + list(args[1:]) )) if False else fn(bad)
            _type_ok = False; _type_err = f"{fn.__name__}({bad!r}) accepted"; break
        except ValueError:
            pass
    if not _type_ok:
        break
check("1g credits_to_millis/millis_to_usd_str reject float/bool/str/None",
      _type_ok, _type_err)
try:
    millis_to_usd_cents_str(10001.0 if False else True)
    check("1h cents formatter rejects bool", False, "accepted True")
except ValueError:
    check("1h cents formatter rejects bool", True)
try:
    millis_to_usd_cents_str(10001)
    check("1i cents formatter fails closed on sub-cent input", False, "accepted 10001")
except ValueError as e:
    check("1i cents formatter fails closed on sub-cent input",
          str(e) == "sub_cent_amount", str(e))
check("1j lossless display: 10001 → '$10.001'",
      millis_to_usd_str(10001) == "$10.001")


# ── fixtures: consumer + provider + tools + real settlement via gate.run ──
import json as _json2
import asyncio as _asyncio
from execution.results import StagedItems, StagedFileItem
from execution.invocation import ToolInvocation, SourceFile

def _user(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x',"
            " 'T', 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    return int(row["id"])

caller = _user(_u + "caller")
owner = _user(_u + "owner")
admin = 1
from economy.billing import grant as _grant
_grant(caller, 100000, "grant_package", note="s3 fixture")

T3, T1P = f"{_u}o_s3third", f"s3first{_u}"   # third-party owner ns; first-party bare id
def _mktool(tk, own, cost):
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
            " label_json, visibility, enabled,"
            " params_json, endpoint_json, created_at, updated_at, accept_json, format_family) values"
            " (%s, %s, %s, 'http', %s, 'public',"
            " true, '[]', %s, %s, %s, '[\".png\"]', 'image')",
            (tk, own, tk.split("/")[-1], _json2.dumps({"en": "S3"}),
             _json2.dumps({"url": "https://example.org/x", "timeout_s": 30}), ts, ts))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, %s, true, %s)", (tk, cost, ts))
_mktool(T3, owner, 500)
_mktool(T1P, None, 500)

from repo.ledger import claim_call, insert_provider_earn_if_absent, earn_pool_balance

def _mkstaged(tag):
    return StagedItems(order=["file"], inline=[], files=[StagedFileItem(
        gokapi_id=f"gk_{_u}_{tag}",
        filename="o.bin",
        content_type="application/octet-stream",
        size=10,
        expiry_days=7,
        allowed_downloads=5,
        expire_at=ts + 86400,
        url_hotlink="")],
    ip="127.0.0.1")

def _inv(tool_id, ref):
    return ToolInvocation(tool_id=tool_id, files=[SourceFile("p", "t")],
                          params={},
                          delivery={"expiry_days": 7, "allowed_downloads": 5},
                          client_ref=ref)

_orch_n = [0]
async def _orch():
    _orch_n[0] += 1
    return _mkstaged(f"orch{_orch_n[0]}")

def _run(user_id, tool_id, ref, own):
    from execution import gate as _g
    return _asyncio.run(_g.run(
        {"id": user_id},
        {"id": tool_id, "tool_key": tool_id, "owner_id": own},
        _inv(tool_id, ref), _orch))

def _call_id(tool_id, ref):
    with db() as con:
        return int(con.execute(
            "select id from tool_calls where client_ref=%s", (ref,)).fetchone()["id"])

def _set_rate(v):
    with db() as con:
        con.execute(
            "insert into settings (key, value, updated_at) values"
            " ('commission_rate', %s, %s) on conflict (key) do update set"
            " value = excluded.value, updated_at = excluded.updated_at",
            (str(v), ts))
_set_rate(30)

_ref3, _ref1 = f"{_u}-r3", f"{_u}-r1"
_run(caller, T3, _ref3, owner)      # third-party paid call (30% commission)
_run(caller, T1P, _ref1, None)      # first-party paid call
cid3, cid1 = _call_id(T3, _ref3), _call_id(T1P, _ref1)   # first-party: owner=None

# ── 2. consumer debit is Credits (spendable pool), unchanged ──
with db() as con:
    debit = con.execute(
        "select delta, kind from ledger where user_id=%s and kind='consume'"
        " order by id desc limit 1", (caller,)).fetchone()
check("2 consumer debit = Credits (kind=consume, -500)",
      debit is not None and debit["delta"] == -500, debit)

# ── 3. third-party legs in USD-millis, split math unchanged (30%) ──
with db() as con:
    pe = con.execute(
        "select delta from ledger where user_id=%s and kind='provider_earn'"
        " and ref_id=%s", (owner, str(cid3))).fetchone()
    pc = con.execute(
        "select delta from ledger where kind='platform_commission'"
        " and ref_id=%s", (str(cid3),)).fetchone()
check("3 provider_earn = 350 USD-millis (500 credits −30%)",
      pe is not None and pe["delta"] == 350, pe)
check("3b platform_commission = 150 USD-millis",
      pc is not None and pc["delta"] == 150, pc)

# ── 4. first-party platform_revenue = USD-millis ──
with db() as con:
    pr = con.execute(
        "select delta from ledger where kind='platform_revenue'"
        " and ref_id=%s", (str(cid1),)).fetchone()
check("4 platform_revenue = 500 USD-millis", pr is not None and pr["delta"] == 500, pr)

# ── 5. provider projections all USD ──
s = earnings.earnings_summary(owner)
check("5a summary USD-millis earned=350", s["earned"] == 350, s)
from economy.money import millis_to_usd_str as _m2u
check("5b summary USD string exact", _m2u(s["earned"]) == "$0.35")
tools_rows = earnings.per_tool_revenue(owner)
check("5c per-tool rows present (S2 attribution intact)",
      any(r["tool_key"] == T3 for r in tools_rows), tools_rows)
lines, total = earnings.revenue_lines(owner, limit=50, offset=0)
check("5d revenue lines all USD-millis deltas",
      total >= 1 and all(ln["delta"] == 350 for ln in lines if ln["tool"] == T3),
      (lines, total))

# float can NEVER be truncated into request_withdrawal
try:
    earnings.request_withdrawal(owner, 10000.999)
    check("1k request_withdrawal rejects float", False, "accepted float")
except ValueError as e:
    check("1k request_withdrawal rejects float",
          str(e) == "invalid_amount_type", str(e))
try:
    earnings.request_withdrawal(owner, True)
    check("1l request_withdrawal rejects bool", False, "accepted True")
except ValueError as e:
    check("1l request_withdrawal rejects bool",
          str(e) == "invalid_amount_type", str(e))
# ── 6. withdrawal minimum + parsing ──
from economy.money import millis_to_usd_cents_str
check("6a minimum = $10.00 (10000 millis, cents contractual)",
      earnings.MIN_WITHDRAWAL_MILLIS == 10000
      and millis_to_usd_cents_str(earnings.MIN_WITHDRAWAL_MILLIS) == "$10.00")
# S3 amend: earnings module no longer carries a to_usd wrapper — the only
# formatters are economy.money.millis_to_usd_str / millis_to_usd_cents_str
check("6a2 no to_usd wrapper in economy.earnings (single authority)",
      not hasattr(earnings, "to_usd"))
check("6b '10.25' → request amount 10250",
      parse_usd_decimal("10.25") == 10250)
try:
    earnings.request_withdrawal(owner, 10001, note="x")   # sub-cent millis
    check("6c sub-cent amount rejected", False, "no raise")
except ValueError as e:
    check("6c sub-cent amount rejected", str(e) == "sub_cent_amount", str(e))

# top up earnings above minimum, then request $10.25
with db() as con:
    a, _c = claim_call(con, f"{_u}-top-{_os.urandom(3).hex()}", caller, T3, 100,
                       "{}", ts, commission_rate_snapshot=30,
                       invocation_hash="f" * 64)
with db() as con:
    base = earn_pool_balance(con, owner)
    insert_provider_earn_if_absent(con, owner, 20000, base + 20000,
                                   int(a["id"]), f"payout:{a['id']}",
                                   "s3 payout", ts)

from economy.money import parse_usd_decimal as _pusd
# S4: requests require a configured payout channel (frozen at request time)
with db() as con:
    con.execute("UPDATE users SET payout_channel = 'USDT-TRC20:testaddr'"
                " WHERE id = %s", (owner,))
w = earnings.request_withdrawal(owner, _pusd("10.25"), note="s3")
check("6d '10.25' request stored as 10250 USD-millis",
      w["amount"] == 10250 and w["status"] == "pending", w)

# ── 7. lifecycle: approve → ledger withdraw in millis; no credits semantics ──
done = earnings.approve_withdrawal(int(w["id"]), admin_user_id=admin)
with db() as con:
    wd = con.execute(
        "select delta, kind from ledger where user_id=%s and kind='withdraw'"
        " order by id desc limit 1", (owner,)).fetchone()
check("7 approve → ledger withdraw = -10250 USD-millis",
      done["status"] == "approved" and wd["delta"] == -10250, (done, wd))

# ── 8. provider-facing API contract: USD strings only, no raw aliases ──
import json as _json3
_route_src = open(_os.path.join(_REPO, "app", "routes", "earn.py"),
                  encoding="utf-8").read()
check("8a /api/earnings emits only *_usd money fields (no raw earned/"
      "available/pending/withdrawn/delta/amount aliases)",
      '"summary": summary' in _route_src
      and '"earned_usd": millis_to_usd_str' in _route_src
      and '"amount_usd": millis_to_usd_str' in _route_src
      and '"min_withdrawal_usd": millis_to_usd_cents_str' in _route_src
      and 's[k + "_usd"] = to_usd' not in _route_src
      and '"min_withdrawal":' not in _route_src, None)
_js_src = open(_os.path.join(_REPO, "static", "pages", "earnings.js"),
               encoding="utf-8").read()
check("8b earnings.js consumes *_usd only (no /1000, no alias fallback)",
      "/ 1000" not in _js_src and "/1000" not in _js_src
      and "s.earned_usd" in _js_src and "ln.amount_usd" in _js_src
      and "w.amount_usd" in _js_src and "min_withdrawal_usd" in _js_src, None)
# withdraw route: strict string-only gate present in source
check("8c withdraw route enforces isinstance(amount_usd, str)",
      "isinstance(usd_text, str)" in _route_src, None)

# ── 9. HTTP-level string-only proof over the real app ──
import urllib.request as _ur
from config import COOKIE_NAME as _CK
from security import token_hash as _th
_base = f"http://127.0.0.1:{_TEST_PORT}"
_ui9 = _user(_u + "http")
# S4: requests require a payout channel; set one so this proof reaches
# the BALANCE authority (the unit under test here)
with db() as con:
    con.execute("UPDATE users SET payout_channel='USDT-TRC20:nine'"
                " WHERE id=%s", (_ui9,))
_tok9 = _os.urandom(24).hex()
with db() as con:
    con.execute(
        "insert into sessions (token_hash, user_id, created_at, expires_at)"
        " values (%s, %s, %s, %s)",
        (_th(_tok9), _ui9, ts, ts + 3600))
def _post(path, body):
    req = _ur.Request(_base + path, data=_json3.dumps(body).encode(),
                      headers={"Content-Type": "application/json",
                               "Cookie": f"{_CK}={_tok9}"},
                      method="POST")
    try:
        with _ur.urlopen(req, timeout=10) as r:
            return r.status, _json3.loads(r.read())
    except _ur.HTTPError as e:
        return e.code, _json3.loads(e.read())
# a valid string request on this zero-balance user must reach the balance
# authority (withdrawal_insufficient), proving strings DO enter the chain
_code_ok, _body_ok = _post("/api/earnings/withdraw", {"amount_usd": "10.25"})
check("9e valid string reaches balance authority (insufficient, not parse)",
      _code_ok == 422 and _body_ok.get("error") == "withdrawal_insufficient",
      (_code_ok, _body_ok))

_code, _body = _post("/api/earnings/withdraw", {"amount_usd": 10.25})
check("9a JSON number 10.25 → 422 invalid_usd_amount",
      _code == 422 and _body.get("error") == "invalid_usd_amount", (_code, _body))
_code, _body = _post("/api/earnings/withdraw", {"amount_usd": 10})
check("9b JSON number 10 → 422 invalid_usd_amount",
      _code == 422 and _body.get("error") == "invalid_usd_amount", (_code, _body))
_code, _body = _post("/api/earnings/withdraw", {"amount_usd": "10.001"})
check("9c string '10.001' → 422 sub_cent_amount",
      _code == 422 and _body.get("error") == "sub_cent_amount", (_code, _body))
_code, _body = _post("/api/earnings/withdraw", {"amount_usd": "abc"})
check("9d malformed string → 422 invalid_usd_amount",
      _code == 422 and _body.get("error") == "invalid_usd_amount", (_code, _body))

# ── 10. historical sub-cent withdrawal renders losslessly ──
with db() as con:
    con.execute(
        "insert into withdrawals (user_id, amount, status, note, created_at,"
        " payout_channel_snapshot, reviewed_at, reviewed_by)"
        " values (%s, 10001, 'approved', 'historical sub-cent', %s,"
        " 'USDT-TRC20:hist', %s, %s)", (owner, ts, ts, admin))
from repo.economy import withdrawals_by_user as _wby
with db() as con:
    _hist = [w for w in _wby(con, owner, 50, 0) if w["amount"] == 10001]
check("10a historical 10001-milli row present", len(_hist) == 1, _hist)
from economy.money import millis_to_usd_str as _lossless
check("10b provider projection lossless $10.001",
      _lossless(10001) == "$10.001")
_sl_src = open(_os.path.join(_REPO, "app", "routes", "samelabs",
                              "economy.py"),
               encoding="utf-8").read()
check("10c operator queue uses lossless millis_to_usd_str",
      'd["amount_usd"] = millis_to_usd_str(int(d["amount"]))' in _sl_src)
_er_src = open(_os.path.join(_REPO, "app", "routes", "earn.py"),
               encoding="utf-8").read()
check("10d provider history uses lossless millis_to_usd_str",
      '"amount_usd": millis_to_usd_str(w["amount"])' in _er_src)
check("10e minimum still cents-contractual $10.00",
      millis_to_usd_cents_str(earnings.MIN_WITHDRAWAL_MILLIS) == "$10.00")
try:
    earnings.request_withdrawal(owner, 10001, note="sub")   # new = whole-cent only
    check("10f new withdrawal still rejects sub-cent", False, "accepted")
except ValueError as e:
    check("10f new withdrawal still rejects sub-cent",
          str(e) == "sub_cent_amount", str(e))

# ── cleanup: every fixture this suite created (leaked platform_tools
# rows drift the browser gate's market page) ──
with db() as con:
    con.execute("delete from pricing where tool_id like %s", (f"%{_u}%",))
    con.execute("delete from platform_tools where tool_key like %s",
                (f"%{_u}%",))
    con.execute("delete from settings where key='commission_rate'")
    uids = [int(r["id"]) for r in con.execute(
        "select id from users where username like %s",
        (_u + "%",)).fetchall()]
    for uid in uids:
        con.execute("delete from tool_calls where user_id=%s", (uid,))
        con.execute("delete from ledger where user_id=%s", (uid,))
        con.execute("delete from upload_usage where user_id=%s", (uid,))
        con.execute("delete from ingress_requests where user_id=%s", (uid,))
        con.execute("delete from withdrawals where user_id=%s", (uid,))
        purge_users(con, [uid])

print()
if FAIL:
    print(f"FAILED {len(FAIL)}: {FAIL}")
    _sys.exit(1)
print(f"OK — provider earnings USD-milli denomination ({COUNT} checks)")
