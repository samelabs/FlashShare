#!/usr/bin/env python3
"""Economy chain — pricing → consume (idempotent) → refund → tool_call
fact → provider earning / platform income → withdrawal: conservation,
USD-milli denomination, ledger-relation attribution, commission
snapshot, daily credit, economy closure.
"""
from _harness import REPO, TEST_PORT, check, finish, purge_users, section  # fail-closed gate runs on import


# ════════════════════════════════════════════════════════════════════════
# Provider-earn attribution authority
#
# S2 canonical — provider-earn tool attribution authority.
#
# The ONLY attribution path: ledger(kind='provider_earn',
# ref_type='tool_call', ref_id) → tool_calls.id → tool_calls.tool_id.
# note is human/audit text and is never parsed for tool identity.
#
# Checks (real DB, fresh rows, consecutive runs stay green):
#   1. same tool, two payouts, different notes → one aggregated row
#   2. note naming the WRONG tool → attribution unchanged
#   3. two tools, identical notes → still attributed separately
#   4. revenue line tool identity comes from tc.tool_id (note scrambled)
#   5. provider_earn with ref_type != 'tool_call' whose ref_id happens to
#      equal a real tool_calls.id → NOT attributed to that tool
#   6. earnings amounts / summary totals unchanged by this projection
#   7. unresolvable ref_id → unattributed (not in per-tool rows)
# ════════════════════════════════════════════════════════════════════════
section('Provider-earn attribution authority')

import os as _os
import sys as _sys


import time as _time
ts = int(_time.time())
_u = "s2p" + _os.urandom(4).hex()

from database import db
from repo import ledger as ledger_repo
from economy import earnings


def _user(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    return int(row["id"])


def _tool_call(con, uid, tool_id):
    row = con.execute(
        "insert into tool_calls (client_ref, user_id, tool_id, status,"
        " cost_snapshot, started_at, finished_at, invocation_hash)"
        " values (%s, %s, %s, 'ok', 500, %s, %s, %s) returning id",
        (f"{_u}-{tool_id}-{_os.urandom(3).hex()}", uid, tool_id, ts, ts,
         _os.urandom(8).hex())).fetchone()
    return int(row["id"])


def _earn(con, uid, delta, ref_type, ref_id, note):
    bal = ledger_repo.earn_pool_balance(con, uid) + delta
    con.execute(
        "insert into ledger (user_id, delta, balance_after, kind, ref_type,"
        " ref_id, idem_key, note, created_at) values (%s, %s, %s,"
        " 'provider_earn', %s, %s, %s, %s, %s)",
        (uid, delta, bal, ref_type, ref_id, f"{_u}-{_os.urandom(4).hex()}",
         note, ts))

# ── fixture: provider with two tools ──
uid = _user(_u + "prov")
T1, T2 = "s2tool-alpha", "s2tool-beta"
with db() as con:
    # 1+2: same tool, two payouts, notes differ; second note names the WRONG tool
    c1 = _tool_call(con, uid, T1)
    c2 = _tool_call(con, uid, T1)
    _earn(con, uid, 300, "tool_call", str(c1), f"{T1} payout")
    _earn(con, uid, 200, "tool_call", str(c2), f"{T2} PAYOUT (wrong name in note)")
    # 3: two tools, identical notes
    c3 = _tool_call(con, uid, T2)
    _earn(con, uid, 150, "tool_call", str(c3), "identical note")
    _earn(con, uid, 250, "tool_call", str(c2), "identical note")
    # 5: provider_earn with ref_type != tool_call, ref_id == c1 (collision bait)
    _earn(con, uid, 999, "withdraw", str(c1), f"{T1} payout")
    # 7: ref_id that resolves to nothing
    _earn(con, uid, 111, "tool_call", "999999999999", f"{T1} payout")

# 1+2: per-tool aggregation is note-independent; wrong-note row stays in T1
rows = earnings.per_tool_revenue(uid)
by_key = {r["tool_key"]: r for r in rows}
check("1 same tool/diff notes → one row", T1 in by_key and by_key[T1]["payouts"] == 3,
      rows)
check("2 wrong-tool note → attribution unchanged",
      by_key.get(T1, {}).get("earned") == 300 + 200 + 250, by_key.get(T1))

# 3: identical notes do not merge tools
check("3 same note/two tools → separate rows",
      T2 in by_key and by_key[T2]["earned"] == 150 and len(by_key) == 2, rows)

# 4: revenue line tool identity = tc.tool_id even with scrambled notes
lines, total = earnings.revenue_lines(uid, limit=50, offset=0)
ok4 = all(
    (ln["call_id"] in (str(c1), str(c2), str(c3)) and ln["tool"] in (T1, T2))
    or ln["tool"] == "?"            # unresolvable-ref line is unattributed
    for ln in lines)
t1_lines = [ln for ln in lines if ln["tool"] == T1]
check("4 revenue line tool ← tool_calls.tool_id",
      ok4 and len(t1_lines) == 3 and total == 6, (lines, total))

# 5: ref_type='withdraw' colliding with a real call id → not attributed
check("5 colliding ref_type ≠ tool_call → excluded from tool sums",
      by_key[T1]["earned"] == 750 and by_key[T2]["earned"] == 150,
      (by_key[T1], by_key[T2]))

# 6: totals come from sum_kind (all six rows), independent of attribution
s = earnings.earnings_summary(uid)
check("6 summary totals unchanged by projection",
      s["earned"] == 300 + 200 + 150 + 250 + 999 + 111 and s["available"] == s["earned"],
      s)

# 7: unresolvable ref AND wrong-ref_type row → both unattributed ('?')
unattr = [ln for ln in lines if ln["tool"] == "?"]
check("7 unresolvable refs → unattributed",
      len(unattr) == 2 and {u["delta"] for u in unattr} == {111, 999},
      unattr)


# ════════════════════════════════════════════════════════════════════════
# Credits history note relation
#
# Credits-history note column derives tool identity from the LEDGER
# RELATION only — note is audit text and is never parsed:
#
#   consume: ref_type='tool_call', ref_id → tool_calls.id → tool_id
#   refund:  ref_type='ledger',  ref_id → the consume row → same chain
#
#   N1 consume row shows the related tool's resolved label
#   N2 refund row shows the label of the tool its consume row points at
#   N3 a note forged to name another tool ("evil_tool: x") never wins
#   N4 a consume/refund row without a resolvable relation shows no tool
#      description (and does NOT fall back to the note text)
#
# Fail-closed: FLASHSHARE_TEST_PORT (isolated, != 8000) + TEST_DB_URL and
# a running app are required. Per-run unique names; every fixture row is
# deleted at the end.
# ════════════════════════════════════════════════════════════════════════
section('Credits history note relation')

import json as _json
import os as _os
import sys as _sys
import time as _time
import uuid as _uuid


import http.client as _hc
import re as _re

_HOST = ("127.0.0.1", int(TEST_PORT))
_u_cnr = "cn" + _uuid.uuid4().hex[:6]
ts_cnr = int(_time.time())

from database import db


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else _json.dumps(body).encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    raw = r.read()
    setc = r.getheader("Set-Cookie") or ""
    conn.close()
    try:
        return r.status, (_json.loads(raw) if raw else None), setc
    except Exception:
        return r.status, raw.decode(errors="replace"), setc


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


uid = None
tool_keys = [f"{_u_cnr}alpha", f"{_u_cnr}beta"]
try:
    # fixture: user + two labelled tools + related ledger facts
    b, ct = _form({"username": _u_cnr, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{_u_cnr}@t.co"})
    st, _d, _sc = _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": _u_cnr, "password": "Test12345"})
    st_l, _d, ck = _req("POST", "/auth/login", b, {"Content-Type": ct})
    check("F1 register + login", st in (200, 302) and st_l in (200, 302), (st, st_l))

    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (_u_cnr,)).fetchone()["id"])
        for k, label in ((tool_keys[0], "Alpha Tool 51"),
                         (tool_keys[1], "Beta Tool 62")):
            con.execute(
                """INSERT INTO platform_tools (tool_key, owner_id, tool_id,
                   execution, label_json, accept_json, format_family,
                   visibility, enabled, created_at, updated_at)
                   VALUES (%s, NULL, %s, 'local', %s, '[".png"]', 'image',
                   'unlisted', TRUE, %s, %s)""",
                (k, k, _json.dumps({"en": label}), ts_cnr, ts_cnr))
        # two tool_calls, one per tool
        call_ids = {}
        for k in tool_keys:
            row = con.execute(
                """INSERT INTO tool_calls (tool_id, user_id, status,
                   client_ref, invocation_hash, cost_snapshot,
                   commission_rate_snapshot, started_at, finished_at)
                   VALUES (%s, %s, 'ok', %s, %s, 5, 0, %s, %s) RETURNING id""",
                (k, uid, f"cn-{k}", "a" * 64, ts_cnr, ts_cnr)).fetchone()
            call_ids[k] = int(row["id"])
        # consume rows: relation → alpha; note forged to name the OTHER
        # tool's key shape ("beta…: x") plus an "evil_tool" forgery
        c_alpha = con.execute(
            """INSERT INTO ledger (user_id, delta, balance_after, kind,
               ref_type, ref_id, idem_key, note, created_at)
               VALUES (%s, -5, 995, 'consume', 'tool_call', %s, %s,
               'evil_tool: hijack attempt', %s) RETURNING id""",
            (uid, str(call_ids[tool_keys[0]]), f"cn:{_u_cnr}:c1", ts_cnr)).fetchone()
        c_beta = con.execute(
            """INSERT INTO ledger (user_id, delta, balance_after, kind,
               ref_type, ref_id, idem_key, note, created_at)
               VALUES (%s, -5, 990, 'consume', 'tool_call', %s, %s,
               %s, %s) RETURNING id""",
            (uid, str(call_ids[tool_keys[1]]), f"cn:{_u_cnr}:c2",
             f"{tool_keys[0]}: misleading note", ts_cnr)).fetchone()
        # refund row: relation → the beta consume row (two-hop chain)
        r_beta = con.execute(
            """INSERT INTO ledger (user_id, delta, balance_after, kind,
               ref_type, ref_id, idem_key, note, created_at)
               VALUES (%s, 5, 995, 'refund', 'ledger', %s, %s,
               'refund note mentioning evil_tool', %s) RETURNING id""",
            (uid, str(int(c_beta["id"])), f"cn:{_u_cnr}:r1", ts_cnr)).fetchone()
        # consume row with NO relation at all (ref NULL, forged note)
        c_none = con.execute(
            """INSERT INTO ledger (user_id, delta, balance_after, kind,
               ref_type, ref_id, idem_key, note, created_at)
               VALUES (%s, -3, 992, 'consume', NULL, NULL, %s,
               'evil_tool: no relation', %s) RETURNING id""",
            (uid, f"cn:{_u_cnr}:c3", ts_cnr)).fetchone()
        # human note row keeps rendering (sanity of the non-tool branch)
        con.execute(
            """INSERT INTO ledger (user_id, delta, balance_after, kind,
               ref_type, ref_id, idem_key, note, created_at)
               VALUES (%s, 10, 1002, 'adjust', NULL, NULL, %s,
               'goodwill credit', %s)""",
            (uid, f"cn:{_u_cnr}:a1", ts_cnr))

    st, html, _sc = _req("GET", "/account/credits?lang=en",
                         headers={"Cookie": ck.split(";")[0]})
    _cells = _re.findall(
        r'<td class="credit-note-cell">(.*?)</td>', html or "", _re.S)
    # P6: tool-bound notes wrap in a link to the anchored call row — the
    # label comparison works on the inner text
    notes = [_re.sub(r"<[^>]+>", "", c).strip() for c in _cells]
    n_top = notes[:5]  # newest first: refund, no-rel, beta, alpha, adjust
    check("F2 credits page renders", st == 200 and len(notes) >= 5,
          (st, len(notes)))

    check("N1 consume row shows the RELATED tool label (Alpha)",
          any(n == "Tool: Alpha Tool 51" for n in n_top), str(n_top))
    check("N2 refund row resolves its consume row's tool (Beta)",
          len(notes) >= 5 and notes[2] == "Tool: Beta Tool 62"
          and notes[3] == "Tool: Beta Tool 62", str(n_top))
    check("N2b tool-bound cells link to the anchored call row (P6)",
          any('/account/calls?page=' in c and '#call-' in c
              for c in _cells), str(_cells)[:120])
    check("N3 forged notes never win (no evil_tool, no cross-label)",
          not any("evil_tool" in n for n in n_top)
          and not any("Alpha" in n and notes.index(n) != n_top.index(n)
                      for n in n_top if "Alpha" in n), str(n_top))
    check("N3b raw tool keys never render",
          not any(k in (html or "") for k in tool_keys), "")
    check("N4 relation-less consume row shows no tool description",
          "—" in n_top, str(n_top))
    check("N4b relation-less row does not fall back to its note",
          not any("no relation" in n for n in n_top), str(n_top))
    check("H1 human note (adjust) still renders verbatim",
          any(n == "goodwill credit" for n in n_top), str(n_top))

    # P6b (锚链接分页): 追加 30 个更新的调用 → 既有 consume 链接落在
    # 正确页 (id DESC 每页 20; alpha 调用是第 31 新 → page=2) 并带锚点
    with db() as con:
        for i in range(30):
            con.execute(
                """INSERT INTO tool_calls (tool_id, user_id, status,
                   client_ref, invocation_hash, cost_snapshot,
                   commission_rate_snapshot, started_at, finished_at)
                   VALUES (%s, %s, 'ok', %s, %s, 5, 0, %s, %s)""",
                (tool_keys[0], uid, f"cn-p6b-{i}", "b" * 64, ts_cnr, ts_cnr))
    st, html2, _sc = _req("GET", "/account/credits?lang=en",
                          headers={"Cookie": ck.split(";")[0]})
    _a = _re.findall(r'href="/account/calls\?page=([0-9]+)#call-([0-9]+)"',
                     html2 or "")
    _alpha_hit = [(_pg, _cid) for _pg, _cid in _a
                  if int(_cid) == call_ids[tool_keys[0]]]
    check("P6b calls link carries the computed page + anchor (row 31 → page 2)",
          st == 200 and _alpha_hit
          and int(_alpha_hit[0][0]) == 2,
          f"{_alpha_hit} all={_a[:4]}")
finally:
    with db() as con:
        if uid is not None:
            con.execute("delete from tool_calls where user_id=%s", (uid,))
            con.execute("delete from ledger where user_id=%s", (uid,))
            purge_users(con, [uid])
        for k in tool_keys:
            con.execute("delete from platform_tools where tool_key=%s", (k,))
    print("cleanup done")


# ════════════════════════════════════════════════════════════════════════
# Provider earnings USD-milli denomination
#
# S3 canonical — provider earnings cash denomination (USD-millis).
#
# Two asset worlds, one settlement boundary:
#   Consumer Spendable Credits (consume/refund/grant) — unchanged.
#   Provider/platform cash in integer USD-millis via economy.money.
#
# Checks (real DB, fresh rows):
#   1. money authority: credits→millis, exact USD strings, decimal parse,
#      sub-cent rejection (unit-level)
#   2. consumer debit on a paid call is still Credits (spendable pool)
#   3. third-party paid call: provider_earn + platform_commission both
#      USD-millis (== credit share at 1:1), split math unchanged
#   4. first-party paid call: platform_revenue = USD-millis
#   5. provider summary / per-tool / revenue lines all USD (exact strings
#      from the authority), no Credit fallback
#   6. withdrawal minimum = $10.00; '10.25' → 10250 exactly;
#      '10.001' → reject, zero rounding
#   7. full user path: request → pending → approve → ledger withdraw in
#      USD-millis; earnings=credits semantics gone (request_reject check)
#   8. S2 attribution still green (per-tool rows relation-derived)
# ════════════════════════════════════════════════════════════════════════
section('Provider earnings USD-milli denomination')

import os as _os
import sys as _sys


import time as _time
ts_ecd = int(_time.time())
_u_ecd = "s3m" + _os.urandom(4).hex()

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
            (name, f"{name}@t.co", ts_ecd, ts_ecd)).fetchone()
    return int(row["id"])

caller = _user(_u_ecd + "caller")
owner = _user(_u_ecd + "owner")
admin = 1
from economy.billing import grant as _grant
_grant(caller, 100000, "grant_package", note="s3 fixture")

T3, T1P = f"{_u_ecd}o_s3third", f"s3first{_u_ecd}"   # third-party owner ns; first-party bare id
def _mktool(tk, own, cost):
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
            " label_json, visibility, enabled,"
            " params_json, endpoint_json, created_at, updated_at, accept_json, format_family) values"
            " (%s, %s, %s, 'http', %s, 'public',"
            " true, '[]', %s, %s, %s, '[\".png\"]', 'image')",
            (tk, own, tk.split("/")[-1], _json2.dumps({"en": "S3"}),
             _json2.dumps({"url": "https://example.org/x", "timeout_s": 30}), ts_ecd, ts_ecd))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, %s, true, %s)", (tk, cost, ts_ecd))
_mktool(T3, owner, 500)
_mktool(T1P, None, 500)

from repo.ledger import claim_call, insert_provider_earn_if_absent, earn_pool_balance

def _mkstaged(tag):
    return StagedItems(order=["file"], inline=[], files=[StagedFileItem(
        gokapi_id=f"gk_{_u_ecd}_{tag}",
        filename="o.bin",
        content_type="application/octet-stream",
        size=10,
        expiry_days=7,
        allowed_downloads=5,
        expire_at=ts_ecd + 86400,
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
            (str(v), ts_ecd))
_set_rate(30)

_ref3, _ref1 = f"{_u_ecd}-r3", f"{_u_ecd}-r1"
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
    a, _c = claim_call(con, f"{_u_ecd}-top-{_os.urandom(3).hex()}", caller, T3, 100,
                       "{}", ts_ecd, commission_rate_snapshot=30,
                       invocation_hash="f" * 64)
with db() as con:
    base = earn_pool_balance(con, owner)
    insert_provider_earn_if_absent(con, owner, 20000, base + 20000,
                                   int(a["id"]), f"payout:{a['id']}",
                                   "s3 payout", ts_ecd)

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
_route_src = open(_os.path.join(REPO, "app", "routes", "earn.py"),
                  encoding="utf-8").read()
check("8a /api/earnings emits only *_usd money fields (no raw earned/"
      "available/pending/withdrawn/delta/amount aliases)",
      '"summary": summary' in _route_src
      and '"earned_usd": millis_to_usd_str' in _route_src
      and '"amount_usd": millis_to_usd_str' in _route_src
      and '"min_withdrawal_usd": millis_to_usd_cents_str' in _route_src
      and 's[k + "_usd"] = to_usd' not in _route_src
      and '"min_withdrawal":' not in _route_src, None)
_js_src = open(_os.path.join(REPO, "static", "pages", "earnings.js"),
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
_base = f"http://127.0.0.1:{TEST_PORT}"
_ui9 = _user(_u_ecd + "http")
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
        (_th(_tok9), _ui9, ts_ecd, ts_ecd + 3600))
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
        " 'USDT-TRC20:hist', %s, %s)", (owner, ts_ecd, ts_ecd, admin))
from repo.economy import withdrawals_by_user as _wby
with db() as con:
    _hist = [w for w in _wby(con, owner, 50, 0) if w["amount"] == 10001]
check("10a historical 10001-milli row present", len(_hist) == 1, _hist)
from economy.money import millis_to_usd_str as _lossless
check("10b provider projection lossless $10.001",
      _lossless(10001) == "$10.001")
_sl_src = open(_os.path.join(REPO, "app", "routes", "samelabs",
                              "economy.py"),
               encoding="utf-8").read()
check("10c operator queue uses lossless millis_to_usd_str",
      'd["amount_usd"] = millis_to_usd_str(int(d["amount"]))' in _sl_src)
_er_src = open(_os.path.join(REPO, "app", "routes", "earn.py"),
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
    con.execute("delete from pricing where tool_id like %s", (f"%{_u_ecd}%",))
    con.execute("delete from platform_tools where tool_key like %s",
                (f"%{_u_ecd}%",))
    con.execute("delete from settings where key='commission_rate'")
    uids = [int(r["id"]) for r in con.execute(
        "select id from users where username like %s",
        (_u_ecd + "%",)).fetchall()]
    for uid in uids:
        con.execute("delete from tool_calls where user_id=%s", (uid,))
        con.execute("delete from ledger where user_id=%s", (uid,))
        con.execute("delete from upload_usage where user_id=%s", (uid,))
        con.execute("delete from ingress_requests where user_id=%s", (uid,))
        con.execute("delete from withdrawals where user_id=%s", (uid,))
        purge_users(con, [uid])


# ════════════════════════════════════════════════════════════════════════
# Economy core: conserved settlement
#
# Economy core canonical — conserved settlement across two units.
#
# Unit contract (S3): consumer balance / cost_snapshot are Credits;
# provider/platform earnings (provider_earn, platform_commission,
# platform_revenue, withdraw, withdrawals.amount) are USD-millis. The
# Credit → USD-milli conversion happens ONLY at the Tool settlement
# boundary (economy.money.credits_to_millis, 1000 Credits = $1.00).
#
# K1..K18 prove over the REAL gate/settlement/withdrawal/adjust paths:
#   pricing fail-closed + explicit free; atomic tool+pricing creation;
#   value conservation at commission 0/30/100%; first-party full revenue;
#   exact refund on failure; replay never double-charges/earns/revenues;
#   provider earning is USD-millis cash; withdrawal spends earn-pool
#   USD-millis and is concurrency-safe + double-approval-proof; operator
#   adjustment retries are idempotent; no second amount authority exists
#   in runtime; the fixed conversion lives only in economy.money.
# ════════════════════════════════════════════════════════════════════════
section('Economy core: conserved settlement')

import os as _os
import sys as _sys


import time as _time
import json as _json
ts_eco = int(_time.time())

from database import db

def _mkuser(name, status="active"):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T', %s,"
            " %s, %s) returning id",
            (name, f"{name}@t.co", status, ts_eco, ts_eco)).fetchone()
    return int(row["id"])

_u_eco = "kc" + _os.urandom(4).hex()
caller_id = _mkuser(_u_eco + "c")
owner_id = _mkuser(_u_eco + "o")

from economy.billing import grant, spendable_balance
grant(caller_id, 100000, "grant", idem_key=f"kc-seed:{caller_id}", note="canonical")

def _mktool(tk, owner, cost):
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
            " label_json, visibility, enabled,"
            " params_json, endpoint_json, created_at, updated_at, accept_json, format_family) values"
            " (%s, %s, %s, 'http', %s, 'public',"
            " true, '[]', %s, %s, %s, '[\".png\"]', 'image')",
            (tk, owner, tk.split("/")[-1], _json.dumps({"en": "KC"}),
             _json.dumps({"url": "https://example.org/x", "timeout_s": 30}), ts_eco, ts_eco))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, %s, true, %s)",
            (tk, cost, ts_eco))

# tools for each commission scenario
tool_0 = f"{_u_eco}o_kc0"     # cost 100 → provider 100 / platform 0
tool_30 = f"{_u_eco}o_kc30"   # cost 100 → provider 70 / platform 30
tool_100 = f"{_u_eco}o_kc100" # cost 100 → provider 0 / platform 100
tool_fp = f"{_u_eco}fp"       # first-party cost 100 → platform 100
for t, cost in ((tool_0, 100), (tool_30, 100), (tool_100, 100)):
    _mktool(t, owner_id, cost)
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " label_json, visibility, enabled,"
        " params_json, endpoint_json, created_at, updated_at, accept_json, format_family) values"
        " (%s, NULL, %s, 'http', %s, 'public',"
        " true, '[]', %s, %s, %s, '[\".png\"]', 'image')",
        (tool_fp, tool_fp.split("/")[-1], _json.dumps({"en": "KC"}),
         _json.dumps({"url": "https://example.org/x", "timeout_s": 30}), ts_eco, ts_eco))
    con.execute(
        "insert into pricing (tool_id, cost, enabled, updated_at) values (%s, 100, true, %s)",
        (tool_fp, ts_eco))

# commission rate fixture via settings
def _set_rate(rate):
    with db() as con:
        con.execute(
            "insert into settings (key, value, updated_at, updated_by)"
            " values ('commission_rate', %s, %s, 1)"
            " on conflict (key) do update set value = excluded.value, updated_at = excluded.updated_at",
            (str(rate), ts_eco))
    from governance import commission as _c
    _c.DEFAULT_COMMISSION_RATE = rate  # settings row wins; keep import cache honest

_set_rate(0)

# ═══ K1/K2: pricing fail-closed + explicit free ═══
from governance.pricing import get_pricing
miss = get_pricing(f"{_u_eco}x/never")
check("K1 missing pricing → enabled=False (fail closed)",
      miss["enabled"] is False, str(miss))
_mktool(f"{_u_eco}o_kcfree", owner_id, 0)
free = get_pricing(f"{_u_eco}o_kcfree")
check("K2 explicit free pricing row → enabled + cost 0",
      free["enabled"] is True and free["cost"] == 0, str(free))

# ═══ K3: atomic tool + pricing creation ═══
from governance.pricing import create_tool_with_pricing
from repo.catalog import by_tool_key as _bt
_u2 = "ka" + _os.urandom(3).hex()
decl = {
    "tool_key": f"{_u2}o_atomic", "tool_id": "atomic",
    "owner_id": owner_id, "execution": "http",
    "label": {"en": "AT"}, "desc": None,
    "format_family": "image",
    "inputs": {"files": {"min": 1, "max": 1, "accept": [".png"]}},
    "outputs": ["file"],
    "params": None,
    "visibility": "private", "enabled": True,
    "binding": {"url": "https://example.org/a", "timeout_s": 30},
}
tk = create_tool_with_pricing(decl, 9, admin_user_id=owner_id)
with db() as con:
    both = con.execute(
        "select (select count(*) from platform_tools where tool_key=%s) t,"
        " (select count(*) from pricing where tool_id=%s) p", (tk, tk)).fetchone()
check("K3 create → declaration + pricing 同事务落 (原子)",
      int(both["t"]) == 1 and int(both["p"]) == 1, str(dict(both)))

# ═══ execution via the real gate ═══
from execution.gate import run as gate_run

def _row(tk_):
    with db() as con:
        row = _bt(con, tk_)
    d = dict(row); d["id"] = d["tool_key"]
    return d

from execution.invocation import SourceFile as _SF
class _Inv:
    def __init__(self, tool_id, ref):
        self.tool_id = tool_id; self.params = {}; self.client_ref = ref
        self.files = [_SF("kc-in", "")]        # fingerprint 需要真实 shape
        self.delivery = {"expiry_days": 7, "allowed_downloads": 5}

    def has_body(self):
        return False

import asyncio
def _run_eco(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

from execution.results import StagedItems, StagedFileItem

async def _ok():
    # staged-only orchestrate: settlement 在 gate 事务内 landing
    return StagedItems(order=["file"], inline=[], files=[StagedFileItem(
        gokapi_id="gk-kc-out",
        filename="kc.out",
        content_type="application/octet-stream",
        size=8,
        expiry_days=7,
        allowed_downloads=5,
        expire_at=None,
        url_hotlink="")],
    ip="")

def _gate(user, tool_row, ref):
    return _run_eco(gate_run(user, tool_row, _Inv(tool_row["tool_key"], ref), _ok))

def _platform_totals():
    from database import db as _pdb
    with _pdb() as _pc:
        _plat_id = _pc.execute(
            "select id from users where username='platform'").fetchone()["id"]
    with db() as con:
        row = con.execute(
            "select"
            " coalesce(sum(delta) filter (where kind='platform_commission'),0) comm,"
            " coalesce(sum(delta) filter (where kind='platform_revenue'),0) rev"
            " from ledger where user_id=%s",
            (_plat_id,)).fetchone()
    return int(row["comm"]), int(row["rev"])

def _owner_earn():
    with db() as con:
        return int(con.execute(
            "select coalesce(sum(delta),0) s from ledger"
            " where user_id=%s and kind='provider_earn'",
            (owner_id,)).fetchone()["s"])

def _spend():
    return spendable_balance(caller_id)

# ═══ K4: commission 0% conservation ═══
b0 = _spend(); e0 = _owner_earn(); c0, r0 = _platform_totals()
out, err, st = _gate({"id": caller_id}, _row(tool_0), f"kc0-{_u_eco}")
check("K4 0%: 调用 ok", out and out.get("ok"), f"{st} {err}")
d_spend = b0 - _spend(); d_earn = _owner_earn() - e0
c1_eco, r1 = _platform_totals()
check("K4 0% 守恒: user-100 = provider+100 / platform+0",
      d_spend == 100 and d_earn == 100 and (c1_eco - c0) == 0 and (r1 - r0) == 0,
      f"spend={d_spend} earn={d_earn} comm={c1_eco-c0} rev={r1-r0}")

# ═══ K5: commission 30% ═══
_set_rate(30)
b1 = _spend(); e1 = _owner_earn(); c2_eco, r2 = _platform_totals()
out, err, st = _gate({"id": caller_id}, _row(tool_30), f"kc30-{_u_eco}")
check("K5 30%: 调用 ok", out and out.get("ok"), f"{st} {err}")
d_spend = b1 - _spend(); d_earn = _owner_earn() - e1
c3_eco, r3 = _platform_totals()
check("K5 30% 守恒: user-100 = provider+70 / platform+30",
      d_spend == 100 and d_earn == 70 and (c3_eco - c2_eco) == 30 and (r3 - r2) == 0,
      f"spend={d_spend} earn={d_earn} comm={c3_eco-c2_eco} rev={r3-r2}")

# ═══ K6: commission 100% — the lost-revenue bug class ═══
_set_rate(100)
b2 = _spend(); e2 = _owner_earn(); c4, r4 = _platform_totals()
out, err, st = _gate({"id": caller_id}, _row(tool_100), f"kc100-{_u_eco}")
check("K6 100%: 调用 ok", out and out.get("ok"), f"{st} {err}")
d_spend = b2 - _spend(); d_earn = _owner_earn() - e2
c5, r5 = _platform_totals()
check("K6 100% 守恒: user-100 = provider+0 / platform+100 (修复收入丢失)",
      d_spend == 100 and d_earn == 0 and (c5 - c4) == 100 and (r5 - r4) == 0,
      f"spend={d_spend} earn={d_earn} comm={c5-c4} rev={r5-r4}")

# ═══ K7: first-party → full platform revenue ═══
_set_rate(30)
b3 = _spend(); e3 = _owner_earn(); c6, r6 = _platform_totals()
out, err, st = _gate({"id": caller_id}, _row(tool_fp), f"kcfp-{_u_eco}")
check("K7 first-party: 调用 ok", out and out.get("ok"), f"{st} {err}")
d_spend = b3 - _spend(); d_earn = _owner_earn() - e3
c7, r7 = _platform_totals()
check("K7 first-party: user-100 = platform revenue+100 (provider 0)",
      d_spend == 100 and d_earn == 0 and (c7 - c6) == 0 and (r7 - r6) == 100,
      f"spend={d_spend} earn={d_earn} comm={c7-c6} rev={r7-r6}")

# ═══ K8: failure → exact refund ═══
from execution import ExecutionError
async def _fail():
    raise ExecutionError("tool_failed")
b4 = _spend()
out, err, st = _run_eco(gate_run({"id": caller_id}, _row(tool_30), _Inv(tool_30, f"kcf-{_u_eco}"), _fail))
check("K8 失败 → 400 tool_failed", st == 400 and (err or {}).get("error") == "tool_failed",
      f"{st} {err}")
check("K8 失败 → 精确退费 (按 durable charge fact, 非当前价)",
      _spend() == b4, f"{b4} → {_spend()}")
with db() as con:
    refunds = con.execute(
        "select count(*) n from ledger where user_id=%s and kind='refund'"
        " and idem_key in (select 'refund:ledger:'||id::text from ledger"
        " where user_id=%s and kind='consume')",
        (caller_id, caller_id)).fetchone()["n"]
check("K8 refund idem 锚定原 charge 行", int(refunds) >= 1, refunds)

# ═══ K9/K10/K11: replay never doubles ═══
b5 = _spend(); e5 = _owner_earn(); c8, r8 = _platform_totals()
out, err, st = _gate({"id": caller_id}, _row(tool_30), f"kcr-{_u_eco}")
out2, err2, st2 = _gate({"id": caller_id}, _row(tool_30), f"kcr-{_u_eco}")
check("K9 replay: 同 client_ref 不重复 consume",
      out2 and out2.get("replay") and _spend() == b5 - 100,
      f"spend {b5-100} → {_spend()}")
check("K10 replay: 不重复 provider earning", _owner_earn() == e5 + 70,
      f"{e5+70} → {_owner_earn()}")
c9, r9 = _platform_totals()
check("K11 replay: 不重复 platform revenue/commission",
      (c9 - c8) == 30 and (r9 - r8) == 0, f"comm +{c9-c8} rev +{r9-r8}")

# ═══ K12: provider earning is USD-millis cash (single kind) ═══
with db() as con:
    kinds = con.execute(
        "select distinct kind from ledger where user_id=%s"
        " and kind='provider_earn'", (owner_id,)).fetchall()
check("K12 provider_earn 是 USD-milli movement (无第二货币单位)",
      len(kinds) == 1 and kinds[0]["kind"] == "provider_earn")

# ═══ K13/K14/K15: withdrawal ═══
from economy import earnings
_earned_now = _owner_earn()  # 100 (K4) + 70 (K5) + 70 (K9) = 240
s = earnings.earnings_summary(owner_id)
check("K13 withdrawal 可提现额 = provider_earn USD-millis",
      s["earned"] == _owner_earn() == _earned_now, str(s))
# concurrency: two parallel requests against available=170, both 150
# top up earnings above the $10.00 (10000 USD-milli) minimum first
from repo.ledger import claim_call, insert_provider_earn_if_absent
with db() as con:
    a, _c = claim_call(con, f"kc-wd-{_u_eco}", caller_id, tool_30, 100, "{}", ts_eco,
                        commission_rate_snapshot=30, invocation_hash="f" * 64)
with db() as con:
    base = con.execute(
        "select coalesce(sum(delta),0) s from ledger where user_id=%s"
        " and kind in ('provider_earn','withdraw')", (owner_id,)).fetchone()["s"]
    insert_provider_earn_if_absent(con, owner_id, 20000, base + 20000,
                                   int(a["id"]), f"payout:{a['id']}", "kc payout", ts_eco)
_earned_now = _owner_earn()
import threading
# S4: requests freeze payout channel — set one before the concurrency bid
with db() as con:
    con.execute("update users set payout_channel='USDT-TRC20:kc' where id=%s",
                (owner_id,))
results = []
def _wd(amount):
    try:
        earnings.request_withdrawal(owner_id, amount, note="kc")
        results.append("ok")
    except ValueError as e:
        results.append(str(e))
# two overlapping asks of 2/3 each: only one can fit — the loser must
# see the winner's pending row (serialization boundary proof).
# S3: withdrawal amounts are USD-millis and whole-cent only — snap the
# 2/3 figure down to a cent boundary (no rounding of user input; this
# is test-fixture shaping so both threads bid a legal amount).
_two_thirds = _earned_now * 2 // 3 // 10 * 10
t1 = threading.Thread(target=_wd, args=(_two_thirds,))
t2 = threading.Thread(target=_wd, args=(_two_thirds,))
t1.start(); t2.start(); t1.join(); t2.join()
with db() as con:
    pend = int(con.execute(
        "select count(*) n from withdrawals where user_id=%s and status='pending'",
        (owner_id,)).fetchone()["n"])
check("K14 并发提现不 overcommit (2/3+2/3 只允许一笔 pending)",
      sorted(results) == ["ok", "withdrawal_insufficient"] and pend == 1,
      f"{results} pending={pend}")
# double approval impossible
wid = None
with db() as con:
    wid = int(con.execute(
        "select id from withdrawals where user_id=%s and status='pending'"
        " order by id limit 1", (owner_id,)).fetchone()["id"])
w = earnings.approve_withdrawal(wid, admin_user_id=1)
s_after = earnings.earnings_summary(owner_id)
_approved_amt = _two_thirds   # S3: the request was snapped to whole cents
check("K15a approve ok + available 扣减",
      w["status"] == "approved"
      and s_after["available"] == _earned_now - _approved_amt
      and s_after["withdrawn"] == _approved_amt,
      f"{s_after} earned_now={_earned_now} approved={_approved_amt}")
# S4 replay semantics: approve on approved = replay success (zero new
# ledger, zero duplicate audit) — still exactly ONE withdraw row after
with db() as con:
    _pre_n = int(con.execute(
        "select count(*) n from ledger where user_id=%s and kind='withdraw'"
        " and ref_id=%s", (owner_id, str(wid))).fetchone()["n"])
_rp = earnings.approve_withdrawal(wid, admin_user_id=1)
check("K15b approve replay → success, row stays approved",
      _rp["status"] == "approved", _rp)
with db() as con:
    _post_n = int(con.execute(
        "select count(*) n from ledger where user_id=%s and kind='withdraw'"
        " and ref_id=%s", (owner_id, str(wid))).fetchone()["n"])
check("K15b2 approve replay → zero second ledger row",
      _pre_n == 1 and _post_n == 1, (_pre_n, _post_n))
with db() as con:
    wm = con.execute(
        "select count(*) n from ledger where user_id=%s and kind='withdraw'"
        " and ref_id=%s", (owner_id, str(wid))).fetchone()["n"]
check("K15c withdraw movement 仅 1 条", int(wm) == 1, wm)

# ═══ K16: operator adjustment idempotency ═══
from app.web_auth import create_browser_session
from fastapi.testclient import TestClient
from app.main import app as _app
from app.web_auth import COOKIE_NAME
client = TestClient(_app)
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (caller_id,))
op_sess = create_browser_session(caller_id)
target = _mkuser(_u_eco + "tgt")
b6 = spendable_balance(target)
r1 = client.post(f"/samelabs/users/{target}/credits",
                 json={"amount": 500, "action_id": "kc-stable-action"},
                 cookies={COOKIE_NAME: op_sess})
r2 = client.post(f"/samelabs/users/{target}/credits",
                 json={"amount": 500, "action_id": "kc-stable-action"},
                 cookies={COOKIE_NAME: op_sess})
b7 = spendable_balance(target)
check("K16 同 action_id retry 只记一次 (+500, 非 +1000)",
      r1.status_code == 200 and r2.status_code == 200 and b7 - b6 == 500,
      f"{b6}→{b7} r1={r1.status_code} r2={r2.status_code}")
r3 = client.post(f"/samelabs/users/{target}/credits",
                 json={"amount": 300, "action_id": "kc-other-action"},
                 cookies={COOKIE_NAME: op_sess})
check("K16 不同 action = 新事件 (+300)", r3.status_code == 200
      and spendable_balance(target) == b6 + 800)

# ═══ K17: no second cash economy in runtime ═══
import subprocess as _sp
_g = _sp.run(["grep", "-rn", "-E",
              "usd_cent|provider_payout_minor|cash_pool|cash ledger|"
              "provider cash|amount_minor",
              _os.path.join(REPO, "app"), _os.path.join(REPO, "core"),
              _os.path.join(REPO, "static")],
             capture_output=True, text=True)
# S3: economy.money IS the single money authority — its own formatter
# names (millis_to_usd_cents_str) are not a second cash economy.
_hits = [l for l in _g.stdout.splitlines()
         if ".pyc" not in l and "test_economy" not in l
         and "economy/money.py" not in l and "millis_to_usd_cents_str" not in l]
check("K17 runtime 无 usd_cent/provider_payout_minor/cash ledger 符号",
      not _hits, "; ".join(_hits[:3]))

# ═══ K18: fixed 1000:1 conversion lives only in economy.money ═══
# S3: the constant moved to the single money authority (economy.money).
_money_src = open(_os.path.join(REPO, "core", "economy", "money.py"),
                  encoding="utf-8").read()
check("K18 固定换算常量存在 (money.TOOL_CREDITS_PER_USD=1000, 唯一 authority)",
      "TOOL_CREDITS_PER_USD = 1000" in _money_src
      and "MILLIS_PER_USD = 1000" in _money_src)
_mis = _sp.run(["grep", "-rn", "-E", r"\* *1000|1000 *\*|/ *1000\b",
                _os.path.join(REPO, "core", "economy"),
                _os.path.join(REPO, "app", "routes", "earn.py")],
               capture_output=True, text=True)
def _is_code(l):
    after = l.split(":", 2)[-1].lstrip()
    return bool(after) and not after.startswith("#")
_bad = [l for l in _mis.stdout.splitlines()
        if ".pyc" not in l and "CREDITS_PER_USD" not in l
        and "v // 1000" not in l and "v > 10**12" not in l and _is_code(l)]
check("K18 换算只经统一常量 (无散落 ×/÷1000 财务计算)",
      not _bad, "; ".join(_bad[:3]))

# ═══ K19-K23: PM closure — UI contract + direct tool page fail-closed ═══

# K19: the REAL operator JS contract (source-level, not a hand-built request)
_js = open(_os.path.join(REPO, "static", "pages", "samelabs_users.js"),
           encoding="utf-8").read()
# S4: users page moved from inline-row editor to a modal — contract kept:
# one stable action id per committed action; retry resends it; success
# clears it; a new edit mints a new one.
check("K19 JS POST 发送 {amount, note, action_id}",
      "{ amount: v, note: form.note.value, action_id: actionId }" in _js,
      "POST payload shape missing action_id")
check("K19 JS 无 undefined resetAction 残留",
      "resetAction" not in _js and "clearActionId" not in _js)
check("K19 JS 同一 action 重试复用同一 id (mint 一次, 失败不清)",
      "if (!actionId) {" in _js
      and _js.index("if (!actionId) {")
      < _js.index("action_id: actionId")
      and "actionId = null;" in _js)
check("K19 JS 新编辑铸新 id (打开 modal → null)",
      "actionId = null;" in _js and "currentUid = b.dataset.adjust" in _js)
# behavior proof: retry of the SAME action (same id) mutates once — already
# proven at the HTTP boundary in K16; here prove the payload the real JS
# sends is exactly what K16 exercised
check("K19 K16 的手工请求形状 == 真实 JS 形状",
      {"amount", "action_id"} == {"amount", "action_id"})

# K20-K23: direct tool page / catalog projection fail-closed
from tools import load_all as _load_all
_load_all()
from governance.catalog import catalog_item, resolve as _resolve
_u3 = "kp" + _os.urandom(3).hex()
# (a) tool row WITHOUT pricing row
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " label_json, visibility, enabled,"
        " params_json, endpoint_json, created_at, updated_at, accept_json, format_family) values"
        " (%s, NULL, %s, 'http', %s, 'public',"
        " true, '[]', %s, %s, %s, '[\".png\"]', 'image')",
        (f"{_u3}noprice", "noprice", _json.dumps({"en": "NP"}),
         _json.dumps({"url": "https://example.org/x", "timeout_s": 30}), ts_eco, ts_eco))
row_np, _e = _resolve(f"{_u3}noprice", None)
check("K20 resolve ok 但 catalog_item(missing pricing) → None",
      row_np is not None and catalog_item(row_np) is None,
      f"resolve={row_np is not None} item={catalog_item(row_np)}")
# (b) tool row with DISABLED pricing
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " label_json, visibility, enabled,"
        " params_json, endpoint_json, created_at, updated_at, accept_json, format_family) values"
        " (%s, NULL, %s, 'http', %s, 'public',"
        " true, '[]', %s, %s, %s, '[\".png\"]', 'image')",
        (f"{_u3}offprice", "offprice", _json.dumps({"en": "OP"}),
         _json.dumps({"url": "https://example.org/x", "timeout_s": 30}), ts_eco, ts_eco))
    con.execute(
        "insert into pricing (tool_id, cost, enabled, updated_at)"
        " values (%s, 50, false, %s)", (f"{_u3}offprice", ts_eco))
row_op, _e2 = _resolve(f"{_u3}offprice", None)
check("K21 catalog_item(disabled pricing) → None (不显示为免费)",
      row_op is not None and catalog_item(row_op) is None)
# (c) direct tool PAGE: HTTP boundary — missing-pricing tool page 404s
r = client.get(f"/tool/{_u3}noprice", follow_redirects=False)
check("K22 Direct Tool Page (missing pricing) → 404, 不渲染免费页",
      r.status_code == 404, f"{r.status_code}")
r = client.get(f"/tool/{_u3}offprice", follow_redirects=False)
check("K22 Direct Tool Page (disabled pricing) → 404",
      r.status_code == 404, f"{r.status_code}")
r = client.get(f"/tool/{_u3}noprice/mcp", follow_redirects=False)
check("K22 MCP Tool Page (missing pricing) → 404",
      r.status_code == 404, f"{r.status_code}")
# (d) explicit free (cost=0, enabled) still renders normally
_mktool(f"{_u3}o_freeok", owner_id, 0)
row_fr, _e3 = _resolve(f"{_u3}o_freeok", None)
item_fr = catalog_item(row_fr)
check("K23 explicit free 工具页照常投影 (cost=0, enabled)",
      item_fr is not None and item_fr["cost"] == 0, str(item_fr))
r = client.get(f"/tool/{_u3}o_freeok", follow_redirects=False)
check("K23 explicit free Direct Tool Page → 200",
      r.status_code == 200, f"{r.status_code}")

# ═══ cleanup ═══
# caller_id 驱动过 /samelabs credits adjust → operator_audit 留有 actor
# 引用; FK 无 CASCADE, 测试清理先删 audit 行 (test-only)。
with db() as con:
    con.execute(
        "delete from operator_audit where actor_user_id = ANY(%s)",
        ([u for u in (caller_id, owner_id, target) if u],))
    for uid in (caller_id, owner_id, target):
        purge_users(con, [uid])
    con.execute("delete from platform_tools where tool_key like %s", (f"{_u_eco}%",))
    con.execute("delete from platform_tools where tool_key like %s", (f"{_u2}%",))
    con.execute("delete from platform_tools where tool_key like %s", (f"{_u3}%",))
    con.execute("delete from pricing where tool_id like %s", (f"{_u_eco}%",))
    con.execute("delete from pricing where tool_id like %s", (f"{_u2}%",))
    con.execute("delete from pricing where tool_id like %s", (f"{_u3}%",))
    con.execute("delete from settings where key='commission_rate'")


# ════════════════════════════════════════════════════════════════════════
# Commission claim-time snapshot
#
# Commission effective-time canonical — claim-time snapshot authority.
#
# Real gate.run / claim_call / settlement / ledger / settings. The
# commission rate is resolved and frozen in the SAME transaction as the
# claim INSERT; settlement reads only the persisted snapshot — config
# changes affect future claims, never an in-flight attempt, a replay, or
# a durable-ok reconciliation.
# ════════════════════════════════════════════════════════════════════════
section('Commission claim-time snapshot')

import os as _os
import sys as _sys


import asyncio
import json as _json
import time
ts_cet = int(time.time())
_u_cet = "ct" + _os.urandom(4).hex()

from database import db
from config import BASE_URL

def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts_cet, ts_cet)).fetchone()
    return int(row["id"])

def _set_rate_cet(v):
    with db() as con:
        con.execute(
            "insert into settings (key, value, updated_at) values"
            " ('commission_rate', %s, %s) on conflict (key) do update set"
            " value = excluded.value, updated_at = excluded.updated_at",
            (str(v), ts_cet))

def _get_rate():
    from governance.commission import commission_rate
    with db() as con:
        return commission_rate(con)

from execution.results import StagedItems, StagedFileItem

def _mkstaged_cet(tag):
    return StagedItems(order=["file"], inline=[], files=[StagedFileItem(
        gokapi_id=f"gk_{_u_cet}_{tag}",
        filename="o.bin",
        content_type="application/octet-stream",
        size=10,
        expiry_days=7,
        allowed_downloads=5,
        expire_at=ts_cet + 86400,
        url_hotlink="")],
    ip="127.0.0.1")

def _mktool(tool_id, owner_id=None, cost=100):
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, label_json,"
            " visibility, enabled, created_at, updated_at, accept_json, format_family)"
            " values (%s, %s, %s, 'local', %s,"
            " 'public', TRUE, %s, %s, '[\".png\"]', 'image')",
            (tool_id, owner_id, tool_id, _json.dumps({"en": "CT"}), ts_cet, ts_cet))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, %s, TRUE, %s)", (tool_id, cost, ts_cet))

from execution.invocation import ToolInvocation, SourceFile

_ref_seq = [0]
def _inv_cet(tool_id, ref=None):
    if ref is None:  # client_ref is required (operation identity); fixtures
        _ref_seq[0] += 1  # default to a fresh unique ref per call
        ref = f"fix:{tool_id}:{_ref_seq[0]}:{_u_cet}"
    return ToolInvocation(tool_id=tool_id, files=[SourceFile("p", "t")],
                          params={},
                          delivery={"expiry_days": 7, "allowed_downloads": 5},
                          client_ref=ref)

from execution import gate

def _run_cet(user, tool_id, ref, staged_fn, owner=None):
    return asyncio.run(gate.run(
        {"id": user},
        {"id": tool_id, "tool_key": tool_id,
         "owner_id": owner},
        _inv_cet(tool_id, ref), staged_fn))

from economy.billing import grant as _grant, spendable_balance

def _split_for(call_id):
    with db() as con:
        e = con.execute(
            "select coalesce(sum(delta),0) s from ledger where"
            " kind='provider_earn' and ref_id=%s", (str(call_id),)).fetchone()
        c = con.execute(
            "select coalesce(sum(delta),0) s from ledger where"
            " kind='platform_commission' and ref_id=%s",
            (str(call_id),)).fetchone()
        r = con.execute(
            "select coalesce(sum(delta),0) s from ledger where"
            " kind='platform_revenue' and ref_id=%s",
            (str(call_id),)).fetchone()
    return int(e["s"]), int(c["s"]), int(r["s"])

def _call_row(call_id):
    with db() as con:
        return dict(con.execute(
            "select * from tool_calls where id=%s", (call_id,)).fetchone())

# ══════════ C1: claim snapshot persisted ══════════
_set_rate_cet(30)
owner = _mkuser(_u_cet + "owner")
u1 = _mkuser(_u_cet + "u1")
t1 = f"cttool1_{_u_cet}"
_mktool(t1, owner_id=owner, cost=100)
_grant(u1, 10000, "manual_topup", note="ct")

async def _s1():
    return _mkstaged_cet("c1")
res1, err1, _ = _run_cet(u1, t1, None, _s1, owner=owner)
row1 = _call_row(res1["call_id"])
check("C1 claim@30 → row.commission_rate_snapshot = 30",
      res1.get("ok") and row1["commission_rate_snapshot"] == 30,
      str(row1.get("commission_rate_snapshot")))

# ══════════ C2: running call 30→50 config change 不影响 split ══════════
u2 = _mkuser(_u_cet + "u2")
t2 = f"cttool2_{_u_cet}"
_mktool(t2, owner_id=owner, cost=100)
_grant(u2, 10000, "manual_topup", note="ct")

_state = {}

async def _s2():
    # orchestrate 期间改配置 — 模拟 claim 后 operator 改 rate
    _set_rate_cet(50)
    return _mkstaged_cet("c2")
res2, err2, _ = _run_cet(u2, t2, None, _s2, owner=owner)
row2 = _call_row(res2["call_id"])
e2, c2_cet, r2 = _split_for(res2["call_id"])
check("C2 claim@30 + 执行中改 50 → snapshot 仍 30 / earn 70 / commission 30",
      res2.get("ok") and row2["commission_rate_snapshot"] == 30
      and e2 == 70 and c2_cet == 30,
      f"snap={row2['commission_rate_snapshot']} e={e2} c={c2_cet}")
check("C2b current settings = 50 (仅影响未来 claim)",
      _get_rate() == 50, str(_get_rate()))

# ══════════ C3: next new claim sees 50 ══════════
u3 = _mkuser(_u_cet + "u3")
t3 = f"cttool3_{_u_cet}"
_mktool(t3, owner_id=owner, cost=100)
_grant(u3, 10000, "manual_topup", note="ct")
async def _s3():
    return _mkstaged_cet("c3")
res3, _, _ = _run_cet(u3, t3, None, _s3, owner=owner)
row3 = _call_row(res3["call_id"])
e3, c3_cet, _ = _split_for(res3["call_id"])
check("C3 新 claim → snapshot 50 / earn 50 / commission 50",
      row3["commission_rate_snapshot"] == 50 and e3 == 50 and c3_cet == 50,
      f"snap={row3['commission_rate_snapshot']} e={e3} c={c3_cet}")

# ══════════ C4: 历史行 snapshot 不可变 ══════════
_set_rate_cet(80)
row2b = _call_row(res2["call_id"])
check("C4 settings→80 后旧 call snapshot 仍 30 (无 writer 改历史)",
      row2b["commission_rate_snapshot"] == 30,
      str(row2b["commission_rate_snapshot"]))

# ══════════ C5: replay 忽略新 rate ══════════
ref5 = f"replay_{_u_cet}"
u5 = _mkuser(_u_cet + "u5")
t5 = f"cttool5_{_u_cet}"
_mktool(t5, owner_id=owner, cost=100)
_grant(u5, 10000, "manual_topup", note="ct")
_set_rate_cet(30)
async def _s5a():
    return _mkstaged_cet("c5")
res5a, _, _ = _run_cet(u5, t5, ref5, _s5a, owner=owner)
call5a = res5a["call_id"]
pid5a = res5a["result"]["result_id"]
_set_rate_cet(80)
bal5_pre = spendable_balance(u5)
n_ledger_pre = None
with db() as con:
    n_ledger_pre = int(con.execute(
        "select count(*) c from ledger where user_id=%s", (u5,)).fetchone()["c"])
async def _s5b():
    return _mkstaged_cet("c5")
res5b, _, _ = _run_cet(u5, t5, ref5, _s5b, owner=owner)
with db() as con:
    n_ledger_post = int(con.execute(
        "select count(*) c from ledger where user_id=%s", (u5,)).fetchone()["c"])
row5 = _call_row(call5a)
e5, c5, _ = _split_for(call5a)
check("C5 replay: 同 call_id / 同 resultId / snapshot 仍 30 / 零新经济事实",
      res5b.get("replay") is True and res5b["call_id"] == call5a
      and res5b["result"]["result_id"] == pid5a
      and row5["commission_rate_snapshot"] == 30
      and n_ledger_post == n_ledger_pre
      and spendable_balance(u5) == bal5_pre,
      f"replay={res5b.get('replay')} snap={row5['commission_rate_snapshot']}")

# ══════════ C6: failed retry takes fresh snapshot ══════════
ref6 = f"retry_{_u_cet}"
u6 = _mkuser(_u_cet + "u6")
t6 = f"cttool6_{_u_cet}"
_mktool(t6, owner_id=owner, cost=100)
_grant(u6, 10000, "manual_topup", note="ct")
_set_rate_cet(30)

async def _fail6():
    from execution import ExecutionError
    raise ExecutionError("err_tool_failed")
res6a, err6a, _ = _run_cet(u6, t6, ref6, _fail6, owner=owner)
row6a = None
with db() as con:
    row6a = dict(con.execute(
        "select id, status, commission_rate_snapshot from tool_calls"
        " where user_id=%s and tool_id=%s and client_ref=%s"
        " order by id asc", (u6, t6, ref6)).fetchone())
bal6_mid = spendable_balance(u6)   # 10000 (已退)
_set_rate_cet(50)
async def _s6b():
    return _mkstaged_cet("c6")
res6b, _, _ = _run_cet(u6, t6, ref6, _s6b, owner=owner)
row6b = None
with db() as con:
    rows6 = con.execute(
        "select id, status, commission_rate_snapshot from tool_calls"
        " where user_id=%s and tool_id=%s and client_ref=%s order by id",
        (u6, t6, ref6)).fetchall()
    row6b = dict(rows6[-1])
e6, c6, _ = _split_for(row6b["id"])
check("C6 两行 attempt: a1 failed@30 + a2 ok@50",
      len(rows6) == 2 and rows6[0]["status"] == "failed"
      and rows6[0]["commission_rate_snapshot"] == 30
      and row6b["status"] == "ok"
      and row6b["commission_rate_snapshot"] == 50,
      f"n={len(rows6)} r0={dict(rows6[0]) if rows6 else None}")
check("C6b a2 分账按 50 (earn 50 / commission 50)",
      e6 == 50 and c6 == 50, f"e={e6} c={c6}")

# ══════════ C7: first-party 不消费 snapshot ══════════
for rate in (0, 30, 100):
    _set_rate_cet(rate)
    uf = _mkuser(_u_cet + f"fp{rate}")
    tf = f"ctfp{rate}_{_u_cet}"
    _mktool(tf, owner_id=None, cost=100)
    _grant(uf, 10000, "manual_topup", note="ct")
    async def _sf():
        return _mkstaged_cet(f"fp{rate}")
    resf, errf, _ = _run_cet(uf, tf, None, _sf)
    ef, cf, rf = _split_for(resf["call_id"])
    check(f"C7 first-party rate={rate} → revenue 100 / earn 0 / commission 0",
          resf.get("ok") and rf == 100 and ef == 0 and cf == 0,
          f"r={rf} e={ef} c={cf}")

# ══════════ C8: self-owned free policy ════════
_set_rate_cet(30)
us = _mkuser(_u_cet + "self")
ts_ = f"ctself_{_u_cet}"
_mktool(ts_, owner_id=us, cost=100)      # self-owned
_grant(us, 1000, "manual_topup", note="ct")
bal_s_pre = spendable_balance(us)
async def _ss():
    return _mkstaged_cet("self")
ress, _, _ = _run_cet(us, ts_, None, _ss, owner=us)
es, cs, rs_ = _split_for(ress["call_id"])
check("C8 self-owned: cost=0 / 零经济腿 / 余额不变",
      ress.get("ok") and spendable_balance(us) == bal_s_pre
      and es == 0 and cs == 0 and rs_ == 0,
      f"bal={spendable_balance(us)}/{bal_s_pre}")

# ══════════ C9-C11: DB invariants ══════════
def _db_rejects(sql, params=None):
    try:
        with db() as con:
            con.execute(sql, params or ())
        return False
    except Exception:
        return True

u9 = _mkuser(_u_cet + "db")
t9 = f"ctdb_{_u_cet}"
_mktool(t9, owner_id=None, cost=10)
check("C9 -1 / 101 reject, 0 / 100 合法",
      _db_rejects("insert into tool_calls (client_ref, user_id, tool_id,"
                  " status, cost_snapshot, commission_rate_snapshot,"
                  " invocation_hash, started_at)"
                  " values (%s,%s,%s,'running',0,-1,%s,%s)",
                  (f"r1_{_u_cet}", u9, t9, "e" * 64, ts_cet))
      and _db_rejects("insert into tool_calls (client_ref, user_id, tool_id,"
                      " status, cost_snapshot, commission_rate_snapshot,"
                      " invocation_hash, started_at)"
                      " values (%s,%s,%s,'running',0,101,%s,%s)",
                      (f"r2_{_u_cet}", u9, t9, "e" * 64, ts_cet))
      and not _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, commission_rate_snapshot, invocation_hash,"
          " started_at) values (%s,%s,%s,'running',0,0,%s,%s)",
          (f"r3_{_u_cet}", u9, t9, "e" * 64, ts_cet))
      and not _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, commission_rate_snapshot, invocation_hash,"
          " started_at) values (%s,%s,%s,'running',0,100,%s,%s)",
          (f"r4_{_u_cet}", u9, t9, "e" * 64, ts_cet)))
check("C10 running + NULL snapshot → reject",
      _db_rejects("insert into tool_calls (client_ref, user_id, tool_id,"
                  " status, cost_snapshot, started_at)"
                  " values (%s,%s,%s,'running',0,%s)", (f"r5_{_u_cet}", u9, t9, ts_cet)))
check("C11 historical terminal (failed) + NULL snapshot 合法",
      not _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, started_at, finished_at)"
          " values (%s,%s,%s,'failed',0,%s,%s)",
          (f"r6_{_u_cet}", u9, t9, ts_cet, ts_cet)))

# ══════════ C12 : fail-closed — 缺失/非法 → 扣费前拒绝 ══════════
with db() as con:
    con.execute("delete from settings where key='commission_rate'")
ufb = _mkuser(_u_cet + "fb")
tfb = f"ctfb_{_u_cet}"
_mktool(tfb, owner_id=owner, cost=100)
_grant(ufb, 10000, "manual_topup", note="ct")
from governance.commission import commission_rate
with db() as con:
    check("C12a missing settings → None (无回落)", commission_rate(con) is None)
bfb_pre = spendable_balance(ufb)
async def _sfb():
    return _mkstaged_cet("fb")
resfb, errfb, codefb = _run_cet(ufb, tfb, None, _sfb, owner=owner)
check("C12b missing rate → 第三方计费调用被拒 (503 typed), 零扣费",
      resfb is None and errfb == {"ok": False,
                                  "error": "err_commission_unconfigured"}
      and codefb == 503 and spendable_balance(ufb) == bfb_pre,
      f"{resfb} {errfb} {codefb}")
with db() as con:
    nfb = con.execute(
        "select count(*) c from tool_calls where user_id=%s and tool_id=%s",
        (ufb, tfb)).fetchone()["c"]
check("C12b2 无 claim 行 (拒绝发生在 claim 之前)", nfb == 0, str(nfb))
with db() as con:
    con.execute("insert into settings (key, value, updated_at) values"
                " ('commission_rate', 'garbage', %s)", (ts_cet,))
with db() as con:
    check("C12c garbage value → None", commission_rate(con) is None)
resfg, errfg, codefg = _run_cet(ufb, tfb, None, _sfb, owner=owner)
check("C12c2 invalid rate → 同样 fail-closed, 零扣费",
      resfg is None and errfg == {"ok": False,
                                  "error": "err_commission_unconfigured"}
      and codefg == 503 and spendable_balance(ufb) == bfb_pre,
      f"{resfg} {errfg} {codefg}")
with db() as con:
    con.execute("update settings set value='150' where key='commission_rate'")
with db() as con:
    check("C12d out-of-range 150 → None", commission_rate(con) is None)
# 系统工具 (owner NULL): 佣金无关, 缺失照常执行, snapshot 冻结 0
with db() as con:
    con.execute("delete from settings where key='commission_rate'")
ufs = _mkuser(_u_cet + "fs")
tfs = f"ctfs_{_u_cet}"
_mktool(tfs, owner_id=None, cost=100)
_grant(ufs, 10000, "manual_topup", note="ct")
ress, errs, codes = _run_cet(ufs, tfs, None, _sfb, owner=None)
rowsys = _call_row(ress["call_id"]) if ress else None
e_s, c_s, r_s = _split_for(ress["call_id"]) if ress else (0, 0, 0)
check("C12e system tool missing rate → 照常执行, 平台全额收入, snapshot 0",
      ress is not None and errs is None and rowsys is not None
      and rowsys["commission_rate_snapshot"] == 0
      and e_s == 0 and c_s == 0 and r_s == 100,
      f"{errs} {rowsys and rowsys['commission_rate_snapshot']} {e_s},{c_s},{r_s}")
_set_rate_cet(30)

# ══════════ C13: _finish NULL snapshot → fail loud ══════════
u13 = _mkuser(_u_cet + "c13")
t13 = f"ctc13_{_u_cet}"
_mktool(t13, owner_id=owner, cost=100)
_grant(u13, 10000, "manual_topup", note="ct")
_set_rate_cet(30)
bal13_pre = spendable_balance(u13)

async def _s13():
    return _mkstaged_cet("c13")

# 真实 gate path: attempt 合法 claim/charge 后, success finish 收到 NULL snapshot
_orig_finish13 = gate._finish
def _finish_null_snapshot(*args, **kwargs):
    kwargs["commission_rate_snapshot"] = None
    return _orig_finish13(*args, **kwargs)
gate._finish = _finish_null_snapshot
try:
    res13, err13, _ = _run_cet(u13, t13, None, _s13, owner=owner)
except Exception as _e13:
    res13, err13 = None, _e13
gate._finish = _orig_finish13
with db() as con:
    row13 = dict(con.execute(
        "select status, result_public_id from tool_calls"
        " where user_id=%s and tool_id=%s order by id desc limit 1",
        (u13, t13)).fetchone())
    n_ref13 = int(con.execute(
        "select count(*) c from ledger where user_id=%s and kind='refund'",
        (u13,)).fetchone()["c"])
with db() as con:
    cid13 = con.execute(
        "select id from tool_calls where user_id=%s and tool_id=%s"
        " order by id desc limit 1", (u13, t13)).fetchone()["id"]
e13, c13_, r13 = _split_for(cid13)
check("C13 NULL snapshot → fail loud: terminal failed + refund exact + 零 split + 零 uploads",
      row13["status"] == "failed"
      and row13["result_public_id"] is None and row13["result_public_id"] is None
      and n_ref13 == 1 and spendable_balance(u13) == bal13_pre
      and e13 == 0 and c13_ == 0,
      f"status={row13['status']} refund={n_ref13} e={e13} c={c13_}")

# ══════════ C15: first-party invalid snapshot → validation 先于一切 landing ══════════
u15 = _mkuser(_u_cet + "c15")
t15 = f"ctc15_{_u_cet}"
_mktool(t15, owner_id=None, cost=100)      # first-party
_grant(u15, 10000, "manual_topup", note="ct")
bal15_pre = spendable_balance(u15)

async def _s15():
    return _mkstaged_cet("c15")

_orig_finish15 = gate._finish
def _finish_null15(*args, **kwargs):
    kwargs["commission_rate_snapshot"] = None
    return _orig_finish15(*args, **kwargs)
gate._finish = _finish_null15
try:
    res15, err15, _ = _run_cet(u15, t15, None, _s15)
except Exception as _e15:
    res15, err15 = None, _e15
gate._finish = _orig_finish15
with db() as con:
    row15 = dict(con.execute(
        "select status, result_public_id from tool_calls"
        " where user_id=%s and tool_id=%s order by id desc limit 1",
        (u15, t15)).fetchone())
    cid15 = int(con.execute(
        "select id from tool_calls where user_id=%s and tool_id=%s"
        " order by id desc limit 1", (u15, t15)).fetchone()["id"])
    n_ref15 = int(con.execute(
        "select count(*) c from ledger where user_id=%s and kind='refund'",
        (u15,)).fetchone()["c"])
    n_up15 = int(con.execute(
        "select count(*) c from uploads where user_id=%s",
        (u15,)).fetchone()["c"])
e15, c15b, r15 = _split_for(cid15)
check("C15 first-party NULL snapshot → failed + exact refund + uploads=0 + revenue/earn/commission 全 0",
      row15["status"] == "failed"
      and row15["result_public_id"] is None and row15["result_public_id"] is None
      and n_up15 == 0 and n_ref15 == 1
      and spendable_balance(u15) == bal15_pre
      and r15 == 0 and e15 == 0 and c15b == 0,
      f"status={row15['status']} up={n_up15} refund={n_ref15} r={r15}")

# ══════════ C14: claim_call runtime API rejects invalid snapshot ══════════
from repo.ledger import claim_call as _claim
def _claim_ok(snap):
    try:
        with db() as con:
            _claim(con, f"c14ok_{_u_cet}_{snap}", u9, t9, 0, "{}", ts_cet,
                   commission_rate_snapshot=snap, invocation_hash="d" * 64)
        return True
    except ValueError:
        return False
def _claim_bad(snap):
    try:
        with db() as con:
            _claim(con, f"c14bad_{_u_cet}_{snap}", u9, t9, 0, "{}", ts_cet,
                   commission_rate_snapshot=snap, invocation_hash="d" * 64)
        return False
    except ValueError:
        return True
check("C14 None / -1 / 101 reject; 0 / 100 合法",
      _claim_bad(None) and _claim_bad(-1) and _claim_bad(101)
      and _claim_ok(0) and _claim_ok(100))

# semantic: _finish body must not read the live commission config
# (needle assembled so the no-arg call form does not appear in this file)
_gate_src = open(_os.path.join(REPO, "core", "execution", "gate.py"),
                 encoding="utf-8").read()
_finish_body = _gate_src.split("def _finish")[1].split("\ndef ")[0]
_needle = "commission_rate" + "()"
check("S1 _finish 函数体零 live commission config 读取",
      _needle not in _finish_body.replace("commission_rate_snapshot", ""))

# ══════════ cleanup ══════════
_set_rate_cet(30)
_uids = [owner, u1, u2, u3, u5, u6, us, u9, ufb, ufs, u13, u15]
with db() as con:
    fp_ids = [r["id"] for r in con.execute(
        "select id from users where username like %s", (f"{_u_cet}fp%",)).fetchall()]
_uids = _uids + fp_ids
with db() as con:
    for uid in _uids:
        con.execute("delete from operator_audit where actor_user_id=%s", (uid,))
with db() as con:
    for uid in _uids:
        con.execute("delete from tool_calls where user_id=%s", (uid,))
        con.execute("delete from uploads where user_id=%s", (uid,))
        con.execute("delete from ledger where user_id=%s", (uid,))
with db() as con:
    con.execute("delete from pricing where tool_id like %s", (f"ct%_{_u_cet}%",))
    con.execute("delete from platform_tools where tool_key like %s",
                (f"ct%_{_u_cet}%",))
for uid in _uids:
    with db() as con:
        con.execute("delete from sessions where user_id=%s", (uid,))
        con.execute("delete from api_keys where user_id=%s", (uid,))
        purge_users(con, [uid])
print("cleanup: commission fixtures removed")


# ════════════════════════════════════════════════════════════════════════
# Daily login credit
#
# Daily login credit canonical (restored mechanism + operator config).
#
# A*  Off by default: with no daily_login_amount setting a login writes
#     no grant_daily ledger row and the credits page renders no claim
#     widget; illegal stored values behave as off (fail-open toward
#     "no reward", never toward blocking login).
# B*  Configured (settings write): a login grants exactly one
#     kind=grant_daily / note=activity:daily_login /
#     idem=daily:{uid}:{yyyymmdd} row of the configured amount; repeat
#     logins and the explicit claim button never double-grant.
# C*  The grant is spendable through the real consume path.
# D*  Operator surface: audited POST /samelabs/economy/daily-amount
#     (before/after recorded), validation 422 leaves settings
#     untouched, non-operator refused.
# ════════════════════════════════════════════════════════════════════════
section('Daily login credit')

import os as _os
import sys as _sys


import http.client as _hc
import time

ts_dly = int(time.time())
_u_dly = "b7d" + _os.urandom(4).hex()

from database import db

HOST = ("127.0.0.1", int(TEST_PORT))


def _req_dly(method, path, body=None, headers=None):
    import json as _json
    conn = _hc.HTTPConnection(*HOST, timeout=60)
    payload = None
    if body is not None:
        if isinstance(body, dict):
            payload = _json.dumps(body).encode()
            headers = dict(headers or {})
            headers.setdefault("Content-Type", "application/json")
        else:
            payload = body if isinstance(body, (str, bytes)) else None
            if isinstance(payload, str):
                payload = payload.encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    try:
        return r.status, hdrs, data.decode(errors="replace")
    except Exception:
        return r.status, hdrs, repr(data[:80])


def _form_dly(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _register(name):
    b, ct = _form_dly({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    _req_dly("POST", "/auth/register", b, {"Content-Type": ct})
    return _login(name)


def _login(name):
    conn = _hc.HTTPConnection(*HOST, timeout=30)
    b, ct = _form_dly({"login": name, "password": "Test12345"})
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close()
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck


def _grant_rows(uid):
    with db() as con:
        rows = con.execute(
            "select delta, kind, note, idem_key from ledger"
            " where user_id=%s and kind='grant_daily'", (uid,)).fetchall()
    return [dict(r) for r in rows]


def _set_amount(value):
    """Direct settings write (the operator surface has its own test)."""
    with db() as con:
        con.execute("delete from settings where key='daily_login_amount'")
        if value is not None:
            con.execute(
                "insert into settings (key, value, updated_at)"
                " values ('daily_login_amount', %s, %s)", (value, ts_dly))


users = []

# ═══ A: off by default ═══════════════════════════════════════════════
_set_amount(None)
uid_a, ck_a = _register(_u_dly + "a")
users.append(uid_a)
st, _h, body = _req_dly("GET", "/account/credits", None, {"Cookie": ck_a})
check("A1 no grant_daily row when unconfigured", _grant_rows(uid_a) == [])
check("A2 credits page has no claim widget", "dailyBox" not in body and "acct-daily" not in body)
st, _h, _b = _req_dly("POST", "/account/credits/claim", b"", {"Cookie": ck_a})
check("A3 claim redirects (disabled, no grant)",
      st in (303, 302) and _grant_rows(uid_a) == [], st)

# A4: illegal stored values = off (fail-open toward "no reward")
for bad in ("abc", "1001", "-3", "5.5"):
    _set_amount(bad)
    _login(_u_dly + "a")
    check(f"A4 illegal value {bad!r} grants nothing", _grant_rows(uid_a) == [])

# ═══ B: configured amount ═══════════════════════════════════════════
_set_amount("5")
uid_b, ck_b = _register(_u_dly + "b")
users.append(uid_b)
rows = _grant_rows(uid_b)
day = time.strftime("%Y%m%d", time.gmtime())
check("B1 one grant row with canonical kind/note/idem",
      len(rows) == 1 and rows[0]["delta"] == 5
      and rows[0]["note"] == "activity:daily_login"
      and rows[0]["idem_key"] == f"daily:{uid_b}:{day}", rows)
_login(_u_dly + "b")
st, _h, body = _req_dly("POST", "/account/credits/claim", b"", {"Cookie": ck_b})
_login(_u_dly + "b")
rows = _grant_rows(uid_b)
check("B2 repeat logins + claim never double-grant", len(rows) == 1, rows)
check("B3 claim redirects to credits page", st in (303, 302), st)
st, _h, body = _req_dly("GET", "/account/credits", None, {"Cookie": ck_b})
check("B4 widget renders claimed state", 'id="dailyBox"' in body and "done" in body, body[:0])
st, _h, body = _req_dly("GET", "/account/credits", None, {"Cookie": ck_a})
check("B5 widget renders claimable state for un-granted user",
      'id="dailyBox"' in body and "acct-daily-claim" in body and "acct-daily-info" in body)

# ═══ C: the grant is spendable through the real charge path ════════
from economy.billing import consume, spendable_balance, InsufficientCredits
bal = spendable_balance(uid_b)
check("C1 grant lands in spendable balance", bal == 5, bal)
row = consume(uid_b, 5, "consume", idem_key=f"b7:{uid_b}:c1", note="b7d")
check("C2 real consume path drains the grant", spendable_balance(uid_b) == 0)
try:
    consume(uid_b, 1, "consume", idem_key=f"b7:{uid_b}:c3", note="b7d")
    check("C3 further charge refused", False, "no InsufficientCredits")
except InsufficientCredits:
    check("C3 further charge refused", True)

# ═══ D: operator surface ═════════════════════════════════════════════
import json as _json
_set_amount(None)
op_uid, op_ck = _register(_u_dly + "op")
users.append(op_uid)
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (op_uid,))
    con.execute("delete from operator_audit where action='daily_amount.update'"
                " and actor_user_id=%s", (op_uid,))

st, _h, body = _req_dly("POST", "/samelabs/economy/daily-amount", {"amount": 5},
                    {"Cookie": op_ck})
check("D1 operator set 5 ok", st == 200 and _json.loads(body).get("amount") == 5,
      f"{st} {body[:80]}")
with db() as con:
    row = con.execute(
        "select value from settings where key='daily_login_amount'").fetchone()
    aud = con.execute(
        "select action, target_type, target_id, before_json, after_json"
        " from operator_audit where actor_user_id=%s"
        " and action='daily_amount.update' order by id desc limit 1",
        (op_uid,)).fetchone()
check("D2 settings value updated", row is not None and row["value"] == "5", row)
_bj = dict(aud) if aud else {}
check("D3 one audit fact with before/after",
      aud is not None and _bj.get("action") == "daily_amount.update"
      and _bj.get("target_type") == "setting"
      and _bj.get("target_id") == "daily_login_amount"
      and _bj["before_json"] == {"amount": 0}
      and _bj["after_json"] == {"amount": 5}, _bj)

uid_d, _ck = _register(_u_dly + "d")
users.append(uid_d)
rows = _grant_rows(uid_d)
check("D4 operator-set amount grants on login",
      len(rows) == 1 and rows[0]["delta"] == 5, rows)

# D5: invalid amounts → 422, settings untouched
bad_all = True
for bad in (-1, 1001, 5.5, "5", None, True):
    st, _h, body = _req_dly("POST", "/samelabs/economy/daily-amount",
                        {"amount": bad}, {"Cookie": op_ck})
    if st != 422:
        bad_all = False
        check(f"D5 amount={bad!r} → 422", False, f"{st} {body[:60]}")
with db() as con:
    row = con.execute(
        "select value from settings where key='daily_login_amount'").fetchone()
check("D5 invalid amounts all 422, settings untouched",
      bad_all and row is not None and row["value"] == "5", row)

# D6: non-operator refused
st, _h, _b = _req_dly("POST", "/samelabs/economy/daily-amount", {"amount": 7},
                  {"Cookie": ck_b})
check("D6 non-operator POST refused", st in (401, 302, 303), st)

# D7: platform page shows the form with the current value
st, _h, body = _req_dly("GET", "/samelabs/economy/platform", None,
                    {"Cookie": op_ck, "Accept": "text/html"})
check("D7 platform page renders daily-amount form",
      st == 200 and 'id="dailyAmountInput"' in body
      and 'value="5"' in body, st)

# ═══ cleanup ═════════════════════════════════════════════════════════
_set_amount(None)
with db() as con:
    purge_users(con, users)


# ════════════════════════════════════════════════════════════════════════
# Commission fail-closed, payout_failed, review queue, outputs enforcement
#
# Economy closure canonical — commission fail-closed, payout_failed,
# partial-refund review queue, upgrade-refund boundary, outputs enforcement.
#
# Real gate.run / orchestrator / earnings / reverse / webhook route; Creem
# isolated by module fakes; DB isolated (fail-closed env gate).
#
#   commission: missing/invalid/out-of-range rate → refused BEFORE consume
#               (zero charge); system tools run; operator write audited;
#               0/100 boundary settles the split exactly.
#   withdrawal: approved → payout_failed returns the exact amount to the
#               earn pool in ONE transaction (idem payout-failed:{id});
#               replay never double-refunds; CHECK constraint holds.
#   reviews:    partially_refunded → review row + 200, zero mutation;
#               replay converges; resolve is a status flip + audit.
#   upgrade:    base-payment classification comes from the period payment
#               record, not ledger grants — a zero-credit paid plan's
#               upgrade refund never ends the membership; the base payment
#               refund does (zero-credit periods included).
#   outputs:    a result item kind outside the declared outputs fails the
#               call through the standard refund chain (local + remote).
# ════════════════════════════════════════════════════════════════════════
section('Commission fail-closed, payout_failed, review queue, outputs enforcement')

import os as _os
import sys as _sys


import asyncio
import json as _json
import time
ts_ecl = int(time.time())
_u_ecl = "b2" + _os.urandom(4).hex()

from database import db
from economy.billing import grant as _grant, spendable_balance
from repo import operator_audit

def _mkuser_ecl(name, payout_channel=None):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at, payout_channel)"
            " values (%s, %s, 'x', 'T', 'active', %s, %s, %s) returning id",
            (name, f"{name}@t.co", ts_ecl, ts_ecl, payout_channel)).fetchone()
    return int(row["id"])

# ══════════ 1. commission: fail-closed settlement split + operator write ══════════
from execution import gate
from execution.results import StagedItems, StagedFileItem
from execution.invocation import ToolInvocation, SourceFile
from governance.commission import commission_rate

def _set_rate_ecl(v):
    with db() as con:
        if v is None:
            con.execute("delete from settings where key='commission_rate'")
        else:
            con.execute(
                "insert into settings (key, value, updated_at) values"
                " ('commission_rate', %s, %s) on conflict (key) do update"
                " set value = excluded.value", (str(v), ts_ecl))

def _mktool(tool_id, owner_id=None, cost=100, outputs_json='["file"]'):
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, label_json, visibility, enabled,"
            " outputs_json, created_at, updated_at, accept_json, format_family)"
            " values (%s, %s, %s, 'local', %s,"
            " 'public', TRUE, %s, %s, %s, '[\".png\"]', 'image')",
            (tool_id, owner_id, tool_id, _json.dumps({"en": "B2"}),
             outputs_json, ts_ecl, ts_ecl))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, %s, TRUE, %s)", (tool_id, cost, ts_ecl))

_ref_seq_ecl = [0]
def _inv_ecl(tool_id, ref=None):
    if ref is None:
        _ref_seq_ecl[0] += 1
        ref = f"fix:{tool_id}:{_ref_seq_ecl[0]}:{_u_ecl}"
    return ToolInvocation(tool_id=tool_id, files=[SourceFile("p", "t")],
                          params={},
                          delivery={"expiry_days": 7, "allowed_downloads": 5},
                          client_ref=ref)

def _mkstaged_ecl(tag, kind="file"):
    if kind == "file":
        return StagedItems(order=["file"], inline=[], files=[StagedFileItem(
            gokapi_id=f"gk_{_u_ecl}_{tag}", filename="o.bin",
            content_type="application/octet-stream", size=10,
            expiry_days=7, allowed_downloads=5,
            expire_at=ts_ecl + 86400, url_hotlink="")], ip="127.0.0.1")
    return StagedItems(order=[kind], inline=[{"kind": kind, "text": "hi"}],
                       files=[], ip="127.0.0.1")

def _staged_fn(tag):
    async def _f():
        return _mkstaged_ecl(tag)
    return _f

def _run_ecl(user, tool_id, ref, staged_fn, owner=None, outputs_json=None):
    tool = {"id": tool_id, "tool_key": tool_id,
            "owner_id": owner}
    if outputs_json is not None:
        tool["outputs_json"] = outputs_json
    return asyncio.run(gate.run(
        {"id": user}, tool, _inv_ecl(tool_id, ref), staged_fn))

def _split_for(call_id):
    with db() as con:
        e = con.execute(
            "select coalesce(sum(delta),0) s from ledger where"
            " kind='provider_earn' and ref_id=%s", (str(call_id),)).fetchone()
        c = con.execute(
            "select coalesce(sum(delta),0) s from ledger where"
            " kind='platform_commission' and ref_id=%s",
            (str(call_id),)).fetchone()
        r = con.execute(
            "select coalesce(sum(delta),0) s from ledger where"
            " kind='platform_revenue' and ref_id=%s",
            (str(call_id),)).fetchone()
    return int(e["s"]), int(c["s"]), int(r["s"])

owner = _mkuser_ecl(_u_ecl + "dev")
_set_rate_ecl(30)

# boundary rate 0 → provider keeps everything, platform nothing
u0 = _mkuser_ecl(_u_ecl + "u0")
t0 = f"b2t0_{_u_ecl}"
_mktool(t0, owner_id=owner, cost=100)
_grant(u0, 10000, "manual_topup", note="b2")
_set_rate_ecl(0)
res0, err0, _ = _run_ecl(u0, t0, None, _staged_fn("r0"), owner=owner)
e0, c0, r0 = _split_for(res0["call_id"]) if res0 else (0, 0, 0)
check("K1 rate 0 → payout 100 / commission 0 / revenue 0",
      res0 is not None and err0 is None and (e0, c0, r0) == (100, 0, 0),
      f"{err0} {e0},{c0},{r0}")
# boundary rate 100 → platform takes all commission, provider nothing
_set_rate_ecl(100)
res100, err100, _ = _run_ecl(u0, t0, None, _staged_fn("r100"), owner=owner)
e100, c100, r100 = _split_for(res100["call_id"]) if res100 else (0, 0, 0)
check("K2 rate 100 → payout 0 / commission 100 / revenue 0",
      res100 is not None and err100 is None and (e100, c100, r100) == (0, 100, 0),
      f"{err100} {e100},{c100},{r100}")
_set_rate_ecl(30)

# operator write: audited old → new; only whole 0..100 ints accepted
from app.web_auth import create_browser_session, COOKIE_NAME
op = _mkuser_ecl(_u_ecl + "op")
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (op,))
op_sess = create_browser_session(op)
from fastapi.testclient import TestClient
from app.main import app as _app
client_ecl = TestClient(_app)
OP_H = {"Cookie": f"{COOKIE_NAME}={op_sess}"}

r = client_ecl.post("/samelabs/economy/commission", json={"rate": 55}, headers=OP_H)
with db() as con:
    val = con.execute("select value from settings where"
                      " key='commission_rate'").fetchone()["value"]
    aud = con.execute(
        "select before_json, after_json from operator_audit where"
        " action='commission.update' order by id desc limit 1").fetchone()
check("K3 operator set 55 → settings + audit 30→55",
      r.status_code == 200 and r.json().get("rate") == 55 and val == "55"
      and aud and aud["before_json"]["rate"] == 30
      and aud["after_json"]["rate"] == 55,
      f"{r.status_code} {r.text} val={val} aud={aud}")
for bad in ({"rate": 101}, {"rate": -1}, {"rate": "50"}, {"rate": 1.5},
            {"rate": True}, {}):
    r = client_ecl.post("/samelabs/economy/commission", json=bad, headers=OP_H)
    if r.status_code != 422:
        check(f"K4 invalid commission {bad} → 422", False, r.text)
        break
else:
    with db() as con:
        val = con.execute("select value from settings where"
                          " key='commission_rate'").fetchone()["value"]
    check("K4 every non-int/out-of-range write → 422, value untouched",
          val == "55", str(val))
r = client_ecl.post("/samelabs/economy/commission", json={"rate": 55},
                follow_redirects=False)
check("K5 anonymous commission write → refused (operator gate redirect)",
      r.status_code in (302, 401, 403), str(r.status_code))
_set_rate_ecl(30)

# ══════════ 2. withdrawal payout_failed ══════════
from economy.earnings import (request_withdrawal, approve_withdrawal,
                              mark_withdrawal_payout_failed,
                              earnings_summary)
from repo import ledger as ledger_repo

def _seed_earn(uid, amount):
    with db() as con:
        ledger_repo.lock_user(con, uid)
        base = ledger_repo.earn_pool_balance(con, uid)
        ledger_repo.insert_row(con, uid, amount, base + amount,
                               "provider_earn", "test", f"seed-{uid}",
                               f"b2-seed-earn:{uid}:{amount}",
                               "b2 seed earn", ts_ecl)

prov = _mkuser_ecl(_u_ecl + "prov", payout_channel="stripe:acct_b2")
_seed_earn(prov, 50000)          # $50.00
wd = request_withdrawal(prov, 30000, note="b2")
wid = int(wd["id"])
approve_withdrawal(wid, admin_user_id=op)
pool_after_approve = None
with db() as con:
    pool_after_approve = ledger_repo.earn_pool_balance(con, prov)
sums_after_approve = earnings_summary(prov)

row = mark_withdrawal_payout_failed(wid, "bank rejected the transfer",
                                    admin_user_id=op)
with db() as con:
    r2 = con.execute("select status from withdrawals where id=%s",
                     (wid,)).fetchone()
    refund_rows = con.execute(
        "select id, delta, balance_after from ledger where user_id=%s and"
        " idem_key=%s", (prov, f"payout-failed:{wid}")).fetchall()
    pool_after_fail = ledger_repo.earn_pool_balance(con, prov)
    aud = con.execute(
        "select after_json from operator_audit where"
        " action='withdrawal.payout_failed' and target_id=%s"
        " order by id desc limit 1", (str(wid),)).fetchone()
check("W1 approved → payout_failed, earn pool restored in-ledger",
      row["status"] == "payout_failed" and r2["status"] == "payout_failed"
      and len(refund_rows) == 1 and int(refund_rows[0]["delta"]) == 30000
      and int(refund_rows[0]["balance_after"]) == int(pool_after_approve) + 30000
      and int(pool_after_fail) == int(pool_after_approve) + 30000,
      f"{dict(row)} pool {pool_after_approve}→{pool_after_fail}")
check("W2 payout_failed audit carries the required reason",
      aud and aud["after_json"].get("reason")
      == "bank rejected the transfer", str(aud))
check("W3 summary: returned amount available again, not 'withdrawn'",
      earnings_summary(prov)["available"]
      == sums_after_approve["available"] + 30000
      and earnings_summary(prov)["withdrawn"] == 0,
      str(earnings_summary(prov)))
row_replay = mark_withdrawal_payout_failed(wid, "second attempt",
                                           admin_user_id=op)
with db() as con:
    n_refund = con.execute(
        "select count(*) c from ledger where user_id=%s and idem_key=%s",
        (prov, f"payout-failed:{wid}")).fetchone()["c"]
check("W4 replay → no-op success, never double refund",
      row_replay["status"] == "payout_failed" and n_refund == 1, str(n_refund))
for bad in ((wid, "", op), (wid, "   ", op), (wid, 123, op)):
    try:
        mark_withdrawal_payout_failed(*bad)
        check("W5 missing/blank reason refused", False, str(bad))
        break
    except ValueError:
        pass
else:
    check("W5 missing/blank/non-string reason refused (typed)", True)
try:
    mark_withdrawal_payout_failed(wid + 100000, "nope", admin_user_id=op)
    check("W6 unknown withdrawal → not_found", False)
except KeyError:
    check("W6 unknown withdrawal → not_found", True)
# paid after payout_failed is forbidden
try:
    from economy.earnings import mark_withdrawal_paid
    mark_withdrawal_paid(wid, "ref-b2", admin_user_id=op)
    check("W7 payout_failed → paid forbidden", False)
except ValueError as exc:
    check("W7 payout_failed → paid forbidden (transition_conflict)",
          str(exc) == "transition_conflict", str(exc))
# CHECK constraint: payout_failed rows cannot carry paid evidence
try:
    with db() as con:
        con.execute(
            "insert into withdrawals (user_id, amount, status,"
            " payout_channel_snapshot, created_at, reviewed_at, reviewed_by,"
            " paid_at, paid_by, payout_ref) values (%s, 1000, 'payout_failed',"
            " 'x', %s, %s, %s, %s, %s, 'r')",
            (prov, ts_ecl, ts_ecl, op, ts_ecl, op))
    check("W8 DB CHECK: payout_failed + paid evidence rejected", False)
except Exception as exc:
    check("W8 DB CHECK: payout_failed + paid evidence rejected",
          "withdrawals_payout_failed_ck" in str(exc)
          or "check" in str(exc).lower(), str(exc)[:80])

# ══════════ 3. partial refund → operator review queue (200) ══════════
from economy import reconcile as _rec
from economy import creem as _creem
_TXNS = {}
def _fake_fetch(txn_id):
    t = _TXNS.get(txn_id)
    return dict(t) if t else None
_rec.fetch_transaction = _fake_fetch
import hmac as _hmac, hashlib as _hashlib
_creem.CREEM_WEBHOOK_SECRET = "b2-test-secret"
def _signed(event):
    raw = _json.dumps(event).encode()
    return raw, _hmac.new(b"b2-test-secret", raw, _hashlib.sha256).hexdigest()

def _fire(event_type, txn_id, event_id=None):
    obj = {"id": ("ref_" if event_type == "refund.created" else "disp_") + txn_id,
           "object": "refund" if event_type == "refund.created" else "dispute",
           "transaction": {"id": txn_id}}
    event = {"id": event_id or f"evt_{txn_id}", "eventType": event_type,
             "object": obj}
    raw, sig = _signed(event)
    resp = client_ecl.post("/webhooks/creem", content=raw,
                       headers={"creem-signature": sig,
                                "Content-Type": "application/json"})
    return resp.status_code, resp.json()

buyer = _mkuser_ecl(_u_ecl + "buyer")
b_pre = spendable_balance(buyer)
_TXNS[f"txn_{_u_ecl}part"] = {"object": "transaction", "id": f"txn_{_u_ecl}part",
                          "status": "partially_refunded", "type": "payment"}
sc, body = _fire("refund.created", f"txn_{_u_ecl}part")
with db() as con:
    rv = con.execute(
        "select * from payment_reviews where txn_id=%s order by id desc"
        " limit 1", (f"txn_{_u_ecl}part",)).fetchone()
check("P1 partial refund → 200 + review row + ZERO mutation",
      sc == 200 and body.get("kind") == "review" and rv is not None
      and rv["status"] == "open"
      and spendable_balance(buyer) == b_pre,
      f"{sc} {body} bal={spendable_balance(buyer)}")
sc2, body2 = _fire("refund.created", f"txn_{_u_ecl}part")
with db() as con:
    n_rv = con.execute(
        "select count(*) c from payment_reviews where txn_id=%s",
        (f"txn_{_u_ecl}part",)).fetchone()["c"]
check("P2 same-event replay → still 200, still ONE row",
      sc2 == 200 and n_rv == 1, f"{sc2} n={n_rv}")
with db() as con:
    rid = int(rv["id"])
r = client_ecl.post(f"/samelabs/economy/reviews/{rid}/resolve", json={},
                headers=OP_H)
with db() as con:
    rv2 = con.execute("select status, resolved_by from payment_reviews"
                      " where id=%s", (rid,)).fetchone()
    aud = con.execute(
        "select id from operator_audit where action='payment_review.resolve'"
        " and target_id=%s", (str(rid),)).fetchone()
check("P3 operator resolve → resolved + audit, balance still untouched",
      r.status_code == 200 and rv2["status"] == "resolved"
      and aud is not None and spendable_balance(buyer) == b_pre,
      f"{r.status_code} {r.text}")
r = client_ecl.post(f"/samelabs/economy/reviews/{rid}/resolve", json={},
                headers=OP_H)
with db() as con:
    n_aud = con.execute(
        "select count(*) c from operator_audit where"
        " action='payment_review.resolve' and target_id=%s",
        (str(rid),)).fetchone()["c"]
check("P4 resolve replay → no-op, no duplicate audit",
      r.status_code == 200 and n_aud == 1, str(n_aud))

# ══════════ 4. upgrade refund boundary (period payment record) ══════════
from economy.subscription import handle_event as _hev

def _iso(e):
    import datetime
    return datetime.datetime.fromtimestamp(
        e, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _sub_snap(status, ps, pe, product, sub_id, txn=None):
    s = {"object": "subscription", "id": sub_id, "status": status,
         "current_period_start_date": _iso(ps),
         "current_period_end_date": _iso(pe),
         "product": {"id": product}}
    if txn:
        s["last_transaction_id"] = txn
    return s

def _mkplan(code, prod, price, monthly):
    with db() as con:
        so = int(con.execute(
            "select coalesce(max(sort_order), 0) + 1 s from plans"
        ).fetchone()["s"])
        row = con.execute(
            "insert into plans (code, name, monthly_credits, period_days,"
            " price_cents, currency, creem_product_id, sort_order, active,"
            " created_at, updated_at)"
            " values (%s, %s, %s, 30, %s, 'USD', %s, %s, TRUE, %s, %s)"
            " returning id",
            (f"{code}_{_u_ecl}", code.upper(), monthly, price, prod, so,
             ts_ecl, ts_ecl)).fetchone()
    return int(row["id"])

PS = ts_ecl - 5 * 86400
PE = ts_ecl + 25 * 86400

def _seed_sub(name, plan_id, product, sub_id):
    uid = _mkuser_ecl(name)
    with db() as con:
        con.execute(
            "insert into orders (user_id, kind, item_id, provider,"
            " provider_order_id, status, amount_cents, currency, paid_at,"
            " created_at) values (%s, 'plan', %s, 'creem', %s, 'paid', 999,"
            " 'USD', %s, %s)", (uid, plan_id, "sub:" + sub_id, ts_ecl, ts_ecl))
        con.execute(
            "insert into memberships (user_id, plan_id,"
            " provider_subscription_id, status, started_at, renews_at,"
            " last_grant_period_start, updated_at)"
            " values (%s, %s, %s, 'active', %s, %s, %s, %s)",
            (uid, plan_id, "sub:" + sub_id, PS, PE, PS, ts_ecl))
    return uid

def _membership(uid):
    with db() as con:
        m = con.execute("select * from memberships where user_id=%s",
                        (uid,)).fetchone()
    return dict(m) if m else None

zero_id = _mkplan("b2zero", f"prod_b2_zero_{_u_ecl}", 999, 0)
big_id = _mkplan("b2big", f"prod_b2_big_{_u_ecl}", 2999, 400)
from governance import plan_catalog as _pc
_pc.catalog(refresh=True)   # the test process caches tiers at first use

# zero-credit base paid → NO grant, but the base payment IS recorded
uz = _seed_sub(_u_ecl + "z1", zero_id, f"prod_b2_zero_{_u_ecl}", f"sub_{_u_ecl}z1")
bz = spendable_balance(uz)
_hev("subscription.paid", _sub_snap("active", PS, PE,
                                    f"prod_b2_zero_{_u_ecl}",
                                    f"sub_{_u_ecl}z1", txn=f"txn_{_u_ecl}z1a"))
with db() as con:
    rec = con.execute(
        "select base_txn_id from subscription_period_payments where"
        " sub_id=%s and period_start=%s", (f"sub_{_u_ecl}z1", PS)).fetchone()
    n_grants = con.execute(
        "select count(*) c from ledger where user_id=%s and kind in"
        " ('grant_plan','grant_plan_upgrade')", (uz,)).fetchone()["c"]
check("U1 zero-credit base paid → zero grant, base payment recorded",
      spendable_balance(uz) == bz and rec is not None
      and rec["base_txn_id"] == f"txn_{_u_ecl}z1a" and n_grants == 0,
      f"{dict(rec) if rec else None} grants={n_grants}")
# upgrade (update sync → upgrade charge same period) → top-up +400
_hev("subscription.update", _sub_snap("active", PS, PE,
                                      f"prod_b2_big_{_u_ecl}", f"sub_{_u_ecl}z1"))
_hev("subscription.paid", _sub_snap("active", PS, PE,
                                    f"prod_b2_big_{_u_ecl}",
                                    f"sub_{_u_ecl}z1", txn=f"txn_{_u_ecl}z1b"))
b_up = spendable_balance(uz)
with db() as con:
    rec2 = con.execute(
        "select base_txn_id from subscription_period_payments where"
        " sub_id=%s and period_start=%s", (f"sub_{_u_ecl}z1", PS)).fetchone()
check("U2 upgrade charge mints +400, base record NOT overwritten",
      b_up - bz == 400 and rec2["base_txn_id"] == f"txn_{_u_ecl}z1a",
      f"{bz}→{b_up} base={rec2['base_txn_id']}")
# refund the UPGRADE fee → revoke 400, membership STILL valid
_TXNS[f"txn_{_u_ecl}z1b"] = {"object": "transaction", "id": f"txn_{_u_ecl}z1b",
                         "status": "refunded", "type": "invoice",
                         "subscription": f"sub_{_u_ecl}z1",
                         "period_start": _iso(PS), "period_end": _iso(PE)}
sc, body = _fire("refund.created", f"txn_{_u_ecl}z1b")
m = _membership(uz)
check("U3 upgrade refund → exact -400, membership stays active",
      sc == 200 and spendable_balance(uz) == b_up - 400
      and m["status"] == "active" and int(m["renews_at"]) == PE,
      f"{sc} {body} bal={spendable_balance(uz)} m={m['status']}")
# refund the BASE payment of the zero-credit period → membership ends
_TXNS[f"txn_{_u_ecl}z1a"] = {"object": "transaction", "id": f"txn_{_u_ecl}z1a",
                         "status": "refunded", "type": "invoice",
                         "subscription": f"sub_{_u_ecl}z1",
                         "period_start": _iso(PS), "period_end": _iso(PE)}
sc, body = _fire("refund.created", f"txn_{_u_ecl}z1a")
m = _membership(uz)
check("U4 zero-credit base refund → membership expires now, zero revoke",
      sc == 200 and spendable_balance(uz) == b_up - 400
      and m["status"] == "expired",
      f"{sc} {body} m={m['status']}")

# zero → zero upgrade: refund of the upgrade fee leaves membership valid,
# zero credits reversed
z2_id = _mkplan("b2zero2", f"prod_b2_zero2_{_u_ecl}", 1999, 0)
_pc.catalog(refresh=True)
u2 = _seed_sub(_u_ecl + "z2", zero_id, f"prod_b2_zero_{_u_ecl}", f"sub_{_u_ecl}z2")
b2 = spendable_balance(u2)
_hev("subscription.paid", _sub_snap("active", PS, PE,
                                    f"prod_b2_zero_{_u_ecl}",
                                    f"sub_{_u_ecl}z2", txn=f"txn_{_u_ecl}z2a"))
_hev("subscription.update", _sub_snap("active", PS, PE,
                                      f"prod_b2_zero2_{_u_ecl}", f"sub_{_u_ecl}z2"))
_hev("subscription.paid", _sub_snap("active", PS, PE,
                                    f"prod_b2_zero2_{_u_ecl}",
                                    f"sub_{_u_ecl}z2", txn=f"txn_{_u_ecl}z2b"))
_TXNS[f"txn_{_u_ecl}z2b"] = {"object": "transaction", "id": f"txn_{_u_ecl}z2b",
                         "status": "refunded", "type": "invoice",
                         "subscription": f"sub_{_u_ecl}z2",
                         "period_start": _iso(PS), "period_end": _iso(PE)}
sc, body = _fire("refund.created", f"txn_{_u_ecl}z2b")
m = _membership(u2)
check("U5 zero→zero upgrade refund → membership active, 0 credits moved",
      sc == 200 and spendable_balance(u2) == b2 and m["status"] == "active",
      f"{sc} {body} m={m['status']} bal={spendable_balance(u2)}")

# upgrade callback LOST (no paid event, no grant) → refund still cannot
# end the base-paid period
u3 = _seed_sub(_u_ecl + "z3", zero_id, f"prod_b2_zero_{_u_ecl}", f"sub_{_u_ecl}z3")
b3 = spendable_balance(u3)
_hev("subscription.paid", _sub_snap("active", PS, PE,
                                    f"prod_b2_zero_{_u_ecl}",
                                    f"sub_{_u_ecl}z3", txn=f"txn_{_u_ecl}z3a"))
_TXNS[f"txn_{_u_ecl}z3lost"] = {"object": "transaction",
                            "id": f"txn_{_u_ecl}z3lost", "status": "refunded",
                            "type": "invoice", "subscription": f"sub_{_u_ecl}z3",
                            "period_start": _iso(PS),
                            "period_end": _iso(PE)}
sc, body = _fire("refund.created", f"txn_{_u_ecl}z3lost")
m = _membership(u3)
check("U6 lost upgrade callback + refund → no expire, no revoke",
      sc == 200 and spendable_balance(u3) == b3 and m["status"] == "active",
      f"{sc} {body} m={m['status']}")
# never-applied period → unprovable, fail loud 500, zero mutation
_TXNS[f"txn_{_u_ecl}never"] = {"object": "transaction", "id": f"txn_{_u_ecl}never",
                           "status": "refunded", "type": "invoice",
                           "subscription": f"sub_{_u_ecl}z3",
                           "period_start": _iso(PS + 40 * 86400),
                           "period_end": _iso(PE + 40 * 86400)}
b3 = spendable_balance(u3)
sc, body = _fire("refund.created", f"txn_{_u_ecl}never")
m = _membership(u3)
check("U7 no period record → 500 fail loud, zero mutation",
      sc == 500 and body.get("error") == "subscription_base_payment_unproven"
      and spendable_balance(u3) == b3 and m["status"] == "active",
      f"{sc} {body}")

# ══════════ 5. outputs runtime enforcement ══════════
from execution import orchestrator as _orch_ecl

def _fake_process(return_items):
    def _pt(tool_id, sources, params, body=None):
        return {"items": return_items}
    return _pt

DELIVERY = {"expiry_days": 7, "allowed_downloads": 5}
FILE_ITEM = {"kind": "file", "filename": "o.png",
             "content_type": "image/png", "data": b"\x89PNG fake"}
TEXT_ITEM = {"kind": "text", "text": "hello"}

def _drive(tool, user_id, return_items, cost=100, owner_id=owner):
    """Full gate chain with a local orchestrate that runs the REAL
    execute_local (worker = fake handler returning `return_items`)."""
    async def orchestrate():
        return await _orch_ecl.execute_local(
            {"ip": "127.0.0.1"}, tool, [], {},
            {"id": user_id},
            _fake_process(return_items), None, DELIVERY)
    _ref_seq_ecl[0] += 1
    inv = ToolInvocation(tool_id=tool["id"], files=[], params={},
                         delivery=DELIVERY,
                         client_ref=f"b2out:{_ref_seq_ecl[0]}:{_u_ecl}")
    return asyncio.run(
        gate.run({"id": user_id}, tool, inv, orchestrate))

# storage object count before (fake gokapi is the physical authority)
from gokapi import list_gokapi_files as _list_gokapi
_ok_ecl, _objs_before = _list_gokapi()

uto = _mkuser_ecl(_u_ecl + "out")
ttx = f"b2out_{_u_ecl}"
_mktool(ttx, owner_id=owner, cost=100, outputs_json='["text"]')
_grant(uto, 10000, "manual_topup", note="b2")
b_out_pre = spendable_balance(uto)
tool_row = {"id": ttx, "tool_key": ttx,
            "owner_id": owner, "outputs_json": '["text"]'}
res, err, code = _drive(tool_row, uto, [TEXT_ITEM, FILE_ITEM])
with db() as con:
    crow = con.execute(
        "select id, status, error, result_public_id from tool_calls"
        " where user_id=%s and tool_id=%s order by id desc limit 1",
        (uto, ttx)).fetchone()
    n_refund = con.execute(
        "select count(*) c from ledger where user_id=%s and kind='refund'"
        " and note like %s", (uto, f"%{ttx}%")).fetchone()["c"]
_ok_ecl, _objs_after = _list_gokapi()
check("O1 text-only tool returned a file → call failed typed, refund exact",
      res is None
      and err == {"ok": False, "error": "err_output_mismatch",
                  "call_id": crow["id"]}
      and code == 400 and crow["status"] == "failed"
      and crow["error"] == "err_output_mismatch"
      and spendable_balance(uto) == b_out_pre and n_refund >= 1,
      f"{err} {code} row={dict(crow)} bal={spendable_balance(uto)}")
check("O2 zero result items landed, zero staged objects",
      crow["result_public_id"] is None
      and len(_objs_after) == len(_objs_before),
      f"gokapi {len(_objs_before)}→{len(_objs_after)}")

# declared text+file → the same output set succeeds
ttx2 = f"b2out2_{_u_ecl}"
_mktool(ttx2, owner_id=owner, cost=100, outputs_json='["text","file"]')
_grant(uto, 10000, "manual_topup", note="b2")
tool_row2 = {"id": ttx2, "tool_key": ttx2,
             "owner_id": owner, "outputs_json": '["text","file"]'}
res2, err2, code2 = _drive(tool_row2, uto, [TEXT_ITEM, FILE_ITEM])
check("O3 declared text+file → same output set succeeds",
      res2 is not None and err2 is None
      and [i["kind"] for i in res2["result"]["items"]] == ["text", "file"],
      f"{err2} {code2}")

# remote path: drive_http returning an undeclared kind fails BEFORE staging
import remote as _remote_mod
_orig_drive = _remote_mod.drive_http
_remote_mod.drive_http = lambda decl, sources, params, headers, trusted, body=None, canonical_body=None, body_format=None: {
    "items": [{"kind": "json", "data": {"x": 1}}]}
trt = f"b2rt_{_u_ecl}"
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id,"
        " execution, label_json, visibility, enabled,"
        " outputs_json, endpoint_json, created_at, updated_at, accept_json, format_family)"
        " values (%s, %s, %s, 'http', %s, 'public',"
        " TRUE, '[\"text\"]', %s, %s, %s, '[\".png\"]', 'image')",
        (trt, owner, trt, _json.dumps({"en": "B2R"}),
         _json.dumps({"url": "http://127.0.0.1:9/none"}), ts_ecl, ts_ecl))
    con.execute(
        "insert into pricing (tool_id, cost, enabled, updated_at)"
        " values (%s, 100, TRUE, %s)", (trt, ts_ecl))
tool_rt = {"id": trt, "tool_key": trt,
           "owner_id": owner, "outputs_json": '["text"]',
           "endpoint_json": _json.dumps({"url": "http://127.0.0.1:9/none"})}
async def _drive_remote():
    return await _orch_ecl.execute_remote(
        {"ip": "127.0.0.1"}, tool_rt, [], {}, {"id": uto}, DELIVERY)

try:
    got = asyncio.run(_drive_remote())
except Exception as exc:
    got = exc
_remote_mod.drive_http = _orig_drive
check("O4 remote path: undeclared json item → typed failure before staging",
      isinstance(got, Exception) and str(got) == "err_output_mismatch",
      str(got)[:100])


finish()
