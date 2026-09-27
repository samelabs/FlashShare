#!/usr/bin/env python3
"""Credits-history note column derives tool identity from the LEDGER
RELATION only — note is audit text and is never parsed:

  consume: ref_type='tool_call', ref_id → tool_calls.id → tool_id
  refund:  ref_type='ledger',  ref_id → the consume row → same chain

  N1 consume row shows the related tool's resolved label
  N2 refund row shows the label of the tool its consume row points at
  N3 a note forged to name another tool ("evil_tool: x") never wins
  N4 a consume/refund row without a resolvable relation shows no tool
     description (and does NOT fall back to the note text)

Fail-closed: FLASHSHARE_TEST_PORT (isolated, != 8000) + TEST_DB_URL and
a running app are required. Per-run unique names; every fixture row is
deleted at the end.
"""
import json as _json
import os as _os
import sys as _sys
import time as _time
import uuid as _uuid

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
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:140]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)


import http.client as _hc
import re as _re

_HOST = ("127.0.0.1", int(_TEST_PORT))
_u = "cn" + _uuid.uuid4().hex[:6]
ts = int(_time.time())

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
tool_keys = [f"{_u}alpha", f"{_u}beta"]
try:
    # fixture: user + two labelled tools + related ledger facts
    b, ct = _form({"username": _u, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{_u}@t.co"})
    st, _d, _sc = _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": _u, "password": "Test12345"})
    st_l, _d, ck = _req("POST", "/auth/login", b, {"Content-Type": ct})
    check("F1 register + login", st in (200, 302) and st_l in (200, 302), (st, st_l))

    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (_u,)).fetchone()["id"])
        for k, label in ((tool_keys[0], "Alpha Tool 51"),
                         (tool_keys[1], "Beta Tool 62")):
            con.execute(
                """INSERT INTO platform_tools (tool_key, owner_id, tool_id,
                   execution, tool_type, label_json, icon,
                   visibility, enabled, created_at, updated_at)
                   VALUES (%s, NULL, %s, 'local', 'produce', %s, 'wand',
                   'unlisted', TRUE, %s, %s)""",
                (k, k, _json.dumps({"en": label}), ts, ts))
        # two tool_calls, one per tool
        call_ids = {}
        for k in tool_keys:
            row = con.execute(
                """INSERT INTO tool_calls (tool_id, user_id, status,
                   client_ref, invocation_hash, cost_snapshot,
                   commission_rate_snapshot, started_at, finished_at)
                   VALUES (%s, %s, 'ok', %s, %s, 5, 0, %s, %s) RETURNING id""",
                (k, uid, f"cn-{k}", "a" * 64, ts, ts)).fetchone()
            call_ids[k] = int(row["id"])
        # consume rows: relation → alpha; note forged to name the OTHER
        # tool's key shape ("beta…: x") plus an "evil_tool" forgery
        c_alpha = con.execute(
            """INSERT INTO ledger (user_id, delta, balance_after, kind,
               ref_type, ref_id, idem_key, note, created_at)
               VALUES (%s, -5, 995, 'consume', 'tool_call', %s, %s,
               'evil_tool: hijack attempt', %s) RETURNING id""",
            (uid, str(call_ids[tool_keys[0]]), f"cn:{_u}:c1", ts)).fetchone()
        c_beta = con.execute(
            """INSERT INTO ledger (user_id, delta, balance_after, kind,
               ref_type, ref_id, idem_key, note, created_at)
               VALUES (%s, -5, 990, 'consume', 'tool_call', %s, %s,
               %s, %s) RETURNING id""",
            (uid, str(call_ids[tool_keys[1]]), f"cn:{_u}:c2",
             f"{tool_keys[0]}: misleading note", ts)).fetchone()
        # refund row: relation → the beta consume row (two-hop chain)
        r_beta = con.execute(
            """INSERT INTO ledger (user_id, delta, balance_after, kind,
               ref_type, ref_id, idem_key, note, created_at)
               VALUES (%s, 5, 995, 'refund', 'ledger', %s, %s,
               'refund note mentioning evil_tool', %s) RETURNING id""",
            (uid, str(int(c_beta["id"])), f"cn:{_u}:r1", ts)).fetchone()
        # consume row with NO relation at all (ref NULL, forged note)
        c_none = con.execute(
            """INSERT INTO ledger (user_id, delta, balance_after, kind,
               ref_type, ref_id, idem_key, note, created_at)
               VALUES (%s, -3, 992, 'consume', NULL, NULL, %s,
               'evil_tool: no relation', %s) RETURNING id""",
            (uid, f"cn:{_u}:c3", ts)).fetchone()
        # human note row keeps rendering (sanity of the non-tool branch)
        con.execute(
            """INSERT INTO ledger (user_id, delta, balance_after, kind,
               ref_type, ref_id, idem_key, note, created_at)
               VALUES (%s, 10, 1002, 'adjust', NULL, NULL, %s,
               'goodwill credit', %s)""",
            (uid, f"cn:{_u}:a1", ts))

    st, html, _sc = _req("GET", "/account/credits?lang=en",
                         headers={"Cookie": ck.split(";")[0]})
    notes = [m.strip() for m in _re.findall(
        r'<td class="credit-note-cell">(.*?)</td>', html or "", _re.S)]
    n_top = notes[:5]  # newest first: refund, no-rel, beta, alpha, adjust
    check("F2 credits page renders", st == 200 and len(notes) >= 5,
          (st, len(notes)))

    check("N1 consume row shows the RELATED tool label (Alpha)",
          any(n == "Tool: Alpha Tool 51" for n in n_top), str(n_top))
    check("N2 refund row resolves its consume row's tool (Beta)",
          len(notes) >= 5 and notes[2] == "Tool: Beta Tool 62"
          and notes[3] == "Tool: Beta Tool 62", str(n_top))
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
finally:
    with db() as con:
        if uid is not None:
            con.execute("delete from tool_calls where user_id=%s", (uid,))
            con.execute("delete from ledger where user_id=%s", (uid,))
            purge_users(con, [uid])
        for k in tool_keys:
            con.execute("delete from platform_tools where tool_key=%s", (k,))
    print("cleanup done")

print()
if FAIL:
    print(f"FAILED {len(FAIL)}: {FAIL}")
    _sys.exit(1)
print(f"OK — credits note relation ({COUNT} checks)")
