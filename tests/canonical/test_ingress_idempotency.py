#!/usr/bin/env python3
"""Ingress idempotency canonical — request receipts for source ingress.

Web /api/uploads (client_ref), REST v1 /uploads (Idempotency-Key) and
MCP tools/call (client_ref) share ONE receipt mechanism: same ref/hash
→ replay the exact stored response with zero new quota/Gokapi; changed
content under the same ref → 409 idempotency_conflict with zero side
effects. Real HTTP surfaces + real DB. Fault injection only.
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
_os.environ.setdefault("FLASHSHARE_GOKAPI_URL", "http://127.0.0.1:53901")
_os.environ.setdefault("FLASHSHARE_GOKAPI_KEY", "test-gk-key")

FAIL = []
COUNT = 0
def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:110]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import base64
import http.client as _hc
import json
import time
import uuid
ts = int(time.time())
_u = "ig" + _os.urandom(3).hex()
_HOST = ("127.0.0.1", int(_TEST_PORT))

from database import db

def _req(method, path, body=None, headers=None, cookies=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=60)
    h = dict(headers or {})
    if cookies:
        h["Cookie"] = cookies
    payload = None
    if body is not None:
        payload = body if isinstance(body, (str, bytes)) else json.dumps(body)
        if isinstance(payload, str):
            payload = payload.encode()
    conn.request(method, path, payload, h)
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    try:
        return r.status, json.loads(raw)
    except Exception:
        return r.status, raw.decode(errors="replace")

# ── fixtures ──
def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"

def _register(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{name}@t.co"})
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    conn.request("POST", "/auth/register", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    if r.status not in (200, 302, 303, 409):
        raise SystemExit(f"register failed: {r.status}")
    conn.close()
    return _login(name)

def _login(name):
    b, ct = _form({"login": name, "password": "Test12345"})
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close()
    return ck

def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    return int(row["id"])

def _usage_n(user_id):
    with db() as con:
        return int(con.execute(
            "select count(*) c from upload_usage where user_id=%s",
            (user_id,)).fetchone()["c"])

def _ingress_n(user_id, ref=None):
    with db() as con:
        if ref:
            return int(con.execute(
                "select count(*) c from ingress_requests where user_id=%s"
                " and request_ref=%s", (user_id, ref)).fetchone()["c"])
        return int(con.execute(
            "select count(*) c from ingress_requests where user_id=%s",
            (user_id,)).fetchone()["c"])

def _gk_count():
    import urllib.request as _ur
    req = _ur.Request("http://127.0.0.1:53901/api/files/list",
                      headers={"apikey": _os.environ.get(
                          "FLASHSHARE_GOKAPI_KEY", "test-gk-key")})
    with _ur.urlopen(req, timeout=5) as r:
        return len(json.loads(r.read()))

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

def _multipart(files, fields):
    b = "----canon"
    parts = []
    for k, v in (fields or {}).items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for fn, content, ct in files:
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{fn}"\r\n'
                     f'Content-Type: {ct}\r\n\r\n'.encode() + content + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"

# ══════════ 1. Web missing client_ref → zero side effects ══════════
ck1 = _register(_u + "w1")
uid1 = None
with db() as con:
    uid1 = con.execute("select id from users where username=%s",
                       (_u + "w1",)).fetchone()["id"]
body, ct = _multipart([("a.png", PNG, "image/png")], {"expiryDays": "1", "allowedDownloads": "50"})
st, r = _req("POST", "/api/uploads", body, {"Content-Type": ct, "Cookie": ck1})
check("1 Web missing client_ref → 422 invalid_request",
      st == 422 and r.get("error") == "invalid_request", f"{st} {str(r)[:80]}")
check("1b zero usage / zero receipts",
      _usage_n(uid1) == 0 and _ingress_n(uid1) == 0)

# ══════════ 2-5. tool-source ingress + replay ══════════
ref2 = f"op-{_u}-2"
body, ct = _multipart([("a.png", PNG, "image/png")],
                      {"tool": "compress", "client_ref": ref2})
gk0 = _gk_count()
st, r = _req("POST", "/api/uploads", body, {"Content-Type": ct, "Cookie": ck1})
check("2 first tool-source ingress → ok",
      st == 200 and r.get("ok") and r.get("file", {}).get("publicId"), f"{st} {str(r)[:90]}")
pid2 = r.get("file", {}).get("publicId")
check("2b one usage + one Gokapi growth + one receipt",
      _usage_n(uid1) == 1 and _gk_count() == gk0 + 1 and _ingress_n(uid1, ref2) == 1,
      f"usage={_usage_n(uid1)} gk={_gk_count()}-{gk0}")

# replay: same ref + same bytes (response-loss retry)
body2, ct = _multipart([("a.png", PNG, "image/png")],
                       {"tool": "compress", "client_ref": ref2})
st, r2 = _req("POST", "/api/uploads", body2, {"Content-Type": ct, "Cookie": ck1})
check("3 same ref/bytes replay → same publicId",
      st == 200 and r2.get("file", {}).get("publicId") == pid2, f"{st} {str(r2)[:90]}")
check("4 replay zero new usage", _usage_n(uid1) == 1)
check("5 replay zero new Gokapi", _gk_count() == gk0 + 1)

# ══════════ 6-7. changed bytes / filename → conflict ══════════
OTHER = PNG + b"\x89PNG\r\n\x1a\n"
body3, ct = _multipart([("a.png", OTHER, "image/png")],
                       {"tool": "compress", "client_ref": ref2})
st, r3 = _req("POST", "/api/uploads", body3, {"Content-Type": ct, "Cookie": ck1})
check("6 same ref changed bytes → 409 idempotency_conflict",
      st == 409 and r3.get("error") == "idempotency_conflict", f"{st} {str(r3)[:80]}")
body4, ct = _multipart([("renamed.png", PNG, "image/png")],
                       {"tool": "compress", "client_ref": ref2})
st, r4 = _req("POST", "/api/uploads", body4, {"Content-Type": ct, "Cookie": ck1})
check("7 same ref changed filename → 409",
      st == 409 and r4.get("error") == "idempotency_conflict", f"{st} {str(r4)[:80]}")
check("7b conflict zero side effects", _usage_n(uid1) == 1 and _gk_count() == gk0 + 1)

# ══════════ 17-19. direct-share response-loss replay ══════════
ck2 = _register(_u + "w2")
with db() as con:
    uid2 = con.execute("select id from users where username=%s",
                       (_u + "w2",)).fetchone()["id"]
ref17 = f"ds-{_u}-1"
body, ct = _multipart([("s.png", PNG, "image/png")],
                      {"expiryDays": "3", "allowedDownloads": "10",
                       "client_ref": ref17})
st, r17 = _req("POST", "/api/uploads", body, {"Content-Type": ct, "Cookie": ck2})
check("17a direct share ok", st == 200 and r17.get("ok") and r17.get("file", {}).get("publicId"),
      f"{st} {str(r17)[:90]}")
pid17 = r17["file"]["publicId"]
with db() as con:
    ups17 = int(con.execute(
        "select count(*) c from uploads where user_id=%s", (uid2,)).fetchone()["c"])
st, r17b = _req("POST", "/api/uploads", body, {"Content-Type": ct, "Cookie": ck2})
check("17 direct-share replay → same publicId",
      st == 200 and r17b.get("file", {}).get("publicId") == pid17, f"{st} {str(r17b)[:90]}")
with db() as con:
    ups17b = int(con.execute(
        "select count(*) c from uploads where user_id=%s", (uid2,)).fetchone()["c"])
check("18 direct-share replay one uploads row", ups17b == ups17 == 1,
      f"{ups17}->{ups17b}")
check("19 direct-share replay one ingress usage", _usage_n(uid2) == 1)

# ══════════ 20-22. REST v1 ══════════
ck3 = _register(_u + "v1")
# bearer token
conn = _hc.HTTPConnection(*_HOST, timeout=30)
conn.request("POST", "/api/tokens", json.dumps({"name": "ig"}).encode(),
             {"Cookie": ck3, "Content-Type": "application/json"})
_tk = json.loads(conn.getresponse().read()); conn.close()
bearer = (_tk.get("token") or {}).get("token") or ""
BH = {"Content-Type": "image/png", "x-filename": "v.png",
      "Authorization": f"Bearer {bearer}"}
with db() as con:
    uid3 = con.execute("select id from users where username=%s",
                       (_u + "v1",)).fetchone()["id"]
st, r = _req("POST", "/api/v1/uploads", PNG, BH)
check("20 v1 missing Idempotency-Key → 422 idempotency_key_required",
      st == 422 and r.get("error") == "idempotency_key_required", f"{st} {str(r)[:80]}")
check("20b zero usage", _usage_n(uid3) == 0)
k21 = f"v1k-{_u}-1"
st, r = _req("POST", "/api/v1/uploads", PNG, {**BH, "Idempotency-Key": k21})
check("21a v1 first upload ok", st == 200 and r.get("ok") and r["file"].get("publicId"),
      f"{st} {str(r)[:90]}")
v1pid = r["file"]["publicId"]
st, r = _req("POST", "/api/v1/uploads", PNG, {**BH, "Idempotency-Key": k21})
check("21 v1 same key+body replay → same handle",
      st == 200 and r["file"]["publicId"] == v1pid, f"{st} {str(r)[:90]}")
st, r = _req("POST", "/api/v1/uploads", OTHER, {**BH, "Idempotency-Key": k21})
check("22 v1 changed body same key → 409",
      st == 409 and r.get("error") == "idempotency_conflict", f"{st} {str(r)[:80]}")

# ══════════ 23-29. MCP ══════════
conn = _hc.HTTPConnection(*_HOST, timeout=30)
conn.request("POST", "/api/tokens", json.dumps({"name": "igmcp"}).encode(),
             {"Cookie": ck3, "Content-Type": "application/json"})
_tk2 = json.loads(conn.getresponse().read()); conn.close()
bearer2 = (_tk2.get("token") or {}).get("token") or bearer

def _mcp(arguments, name="compress", mid=1):
    b = {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}}
    st, r = _req("POST", "/mcp", b, {
        "Content-Type": "application/json", "Authorization": f"Bearer {bearer2}",
        "Accept": "application/json, text/event-stream"})
    # SSE 帧解析
    if isinstance(r, str):
        for line in r.split("\n"):
            if line.startswith("data:"):
                r = json.loads(line[5:])
                break
        else:
            r = json.loads(r)
    return st, r

with db() as con:
    con.execute("update users set status='active' where id=%s", (uid3,))
    con.execute("insert into ledger (user_id, delta, balance_after, kind,"
                " created_at) values (%s, 100000, 100000, 'manual_topup', %s)"
                " on conflict do nothing", (uid3, ts)) if False else None
# credits for MCP metered call
from economy.billing import grant as _grant
_grant(int(uid3), 100000, "manual_topup", note="ig")

gk_mcp0 = _gk_count()
usage_mcp0 = _usage_n(uid3)
ref23 = f"mcp-{_u}-1"
st, r = _mcp({"files": [{"filename": "m.png", "b64": base64.b64encode(PNG).decode()}],
              "params": {}, "delivery": {"expiry_days": 3, "allowed_downloads": 10}})
check("23 MCP missing client_ref → -32602 pre-upload",
      (r.get("error") or {}).get("code") == -32602 and _gk_count() == gk_mcp0,
      str(r)[:100])

st, r = _mcp({"files": [{"filename": "m.png", "b64": base64.b64encode(PNG).decode()}],
              "params": {}, "delivery": {"expiry_days": 3, "allowed_downloads": 10},
              "client_ref": ref23})
_mtxt = ((r.get("result") or {}).get("content") or [{}])[0].get("text", "")
_mres = {}
try:
    _mres = json.loads(_mtxt)
    if isinstance(_mres.get("result"), dict):
        _mres = {**_mres, **_mres["result"]}  # flatten call_id + publicId
except Exception:
    pass
check("24a MCP first call ok (source+execute)",
      bool(_mres.get("publicId")), _mtxt[:110])
mcp_call_id = _mres.get("callId") or _mres.get("call_id")
mcp_pub = _mres.get("publicId")
gk_mcp1 = _gk_count()
usage_mcp1 = _usage_n(uid3)

# exact retry: same ref, same inline file
st, r2 = _mcp({"files": [{"filename": "m.png", "b64": base64.b64encode(PNG).decode()}],
               "params": {}, "delivery": {"expiry_days": 3, "allowed_downloads": 10},
               "client_ref": ref23}, mid=2)
_t2 = ((r2.get("result") or {}).get("content") or [{}])[0].get("text", "")
_mres2 = {}
try:
    _mres2 = json.loads(_t2)
    if isinstance(_mres2.get("result"), dict):
        _mres2 = {**_mres2, **_mres2["result"]}
except Exception:
    pass
check("24 MCP retry same source handles (replay)", bool(_mres2.get("publicId")), _t2[:110])
check("25 MCP retry same call_id/publicId (tool replay)",
      _mres2.get("callId", _mres2.get("call_id")) == mcp_call_id
      and _mres2.get("publicId") == mcp_pub,
      f"{_mres2.get('callId')}/{mcp_call_id} {_mres2.get('publicId')}/{mcp_pub}")
check("26 MCP retry zero Gokapi growth", _gk_count() == gk_mcp1)
check("27 MCP retry zero upload_usage growth", _usage_n(uid3) == usage_mcp1)
from economy.billing import spendable_balance as _sb
_bal_before_retry = _sb(int(uid3))
with db() as con:
    _calls_before = int(con.execute(
        "select count(*) c from tool_calls where user_id=%s",
        (uid3,)).fetchone()["c"])
    _consume_before = int(con.execute(
        "select count(*) c from ledger where user_id=%s and kind='consume'",
        (uid3,)).fetchone()["c"])
# 再来一次 retry (第三次同 ref) — 真实 before/after 差分:
st, r4 = _mcp({"files": [{"filename": "m.png", "b64": base64.b64encode(PNG).decode()}],
               "params": {}, "delivery": {"expiry_days": 3, "allowed_downloads": 10},
               "client_ref": ref23}, mid=4)
with db() as con:
    _calls_after = int(con.execute(
        "select count(*) c from tool_calls where user_id=%s",
        (uid3,)).fetchone()["c"])
    _consume_after = int(con.execute(
        "select count(*) c from ledger where user_id=%s and kind='consume'",
        (uid3,)).fetchone()["c"])
check("28 MCP retry zero second consume/execution (真实差分)",
      _calls_after == _calls_before and _consume_after == _consume_before
      and _sb(int(uid3)) == _bal_before_retry,
      f"calls={_calls_before}->{_calls_after} consume={_consume_before}->{_consume_after}"
      f" bal={_bal_before_retry}->{_sb(int(uid3))}")

# changed inline file same ref → conflict BEFORE upload
st, r3 = _mcp({"files": [{"filename": "m.png", "b64": base64.b64encode(OTHER).decode()}],
               "params": {}, "delivery": {"expiry_days": 3, "allowed_downloads": 10},
               "client_ref": ref23}, mid=3)
check("29 MCP same ref changed inline file → 32009 conflict, zero upload",
      (r3.get("error") or {}).get("message") == "idempotency_conflict"
      and _gk_count() == gk_mcp1,
      str(r3)[:110])

# ══════════ 12. commit-ACK-loss fault injection (真实 finalize 后异常) ══════════
from sharing.ingress_receipt import (
    claim_ingress_quota, finalize_ok, settle_failure, resolve_outcome,
    IngressConflict, IngressInProgress, IngressQuotaDenied,
    OUTCOME_DURABLE_OK, OUTCOME_PROVEN_ROLLBACK, OUTCOME_UNRESOLVED)
from sharing.ingress_identity import ingress_request_hash
import hashlib
# tool-source: receipt finalize 成功 commit, 随后 "ACK 丢失" (调用方收到异常)
# → resolve = DURABLE_OK → 返回 stored result, 零 delete, 零 release
uidT = _mkuser(_u + "ackt")
hT = ingress_request_hash("web_tool_source",
                          [{"filename": "t.png", "content_type": "image/png",
                            "size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}],
                          {"tool_id": "compress"})
refT = f"ack-{_u}-t"
rctT, uidsT, _ = claim_ingress_quota(uidT, "127.0.0.1", refT, hT, [len(PNG)])
usage_T0 = _usage_n(uidT)
finalize_ok(int(rctT["id"]), [{"publicId": "ACKT1", "gokapiId": "ACKT1"}])
# 模拟: finalize commit 后 route 层抛异常, reconciliation 介入
outT = resolve_outcome(int(rctT["id"]))
check("ACK-tool DURABLE_OK → stored result",
      outT["outcome"] == OUTCOME_DURABLE_OK
      and outT["result"][0]["publicId"] == "ACKT1", str(outT)[:100])
check("ACK-tool durable-ok 零 quota release", _usage_n(uidT) == usage_T0 == 1)
# DURABLE_OK 下 replay 而非新 attempt:
_, _, repT = claim_ingress_quota(uidT, "127.0.0.1", refT, hT, [len(PNG)])
check("ACK-tool same ref/hash claim → exact replay", repT == outT["result"])

# direct-share ACK-loss: uploads 落地 + receipt ok 同事务已 commit → reconcile DURABLE_OK
uidD2 = _mkuser(_u + "ackd")
hD2 = ingress_request_hash("web_direct_share",
                           [{"filename": "d.png", "content_type": "image/png",
                             "size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}],
                           {"expiry_days": 3, "allowed_downloads": 10})
refD2 = f"ack-{_u}-d"
rctD2, uidsD2, _ = claim_ingress_quota(uidD2, "127.0.0.1", refD2, hD2, [len(PNG)])
from repo.ingress_requests import finalize_ok as _rpo_final
_direct_rec = [{"publicId": "ACKD1", "gokapiId": "ACKD1", "filename": "d.png"}]
from repo.uploads import insert_record as _up_ins
with db() as con:
    _up_ins(con, uidD2, "127.0.0.1", "ACKD1", "ACKD1", "d.png",
            "image/png", len(PNG), 3, 10, ts + 3 * 86400,
            "http://127.0.0.1:8113/f/ACKD1", "", ts)
    _rpo_final(con, int(rctD2["id"]), json.dumps(_direct_rec), ts)
outD2 = resolve_outcome(int(rctD2["id"]))
check("ACK-direct DURABLE_OK → stored rows replay",
      outD2["outcome"] == OUTCOME_DURABLE_OK
      and outD2["result"][0]["publicId"] == "ACKD1", str(outD2)[:100])
check("ACK-direct durable-ok 零 release / uploads 保留",
      _usage_n(uidD2) == 1)

# UNRESOLVED: receipt running + DB 行不可读模拟 — 直接构造 "finalize 失败且 receipt 仍 running":
uidU = _mkuser(_u + "unres")
hU = ingress_request_hash("v1_source",
                          [{"filename": "u.bin", "content_type": "application/octet-stream",
                            "size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}],
                          {})
rctU, uidsU, _ = claim_ingress_quota(uidU, "127.0.0.1", f"unres-{_u}", hU, [len(PNG)])
outU = resolve_outcome(int(rctU["id"]))
check("UNRESOLVED-probe: running receipt (finalize 未发生) → PROVEN_ROLLBACK",
      outU["outcome"] == OUTCOME_PROVEN_ROLLBACK)
# guarded: settle_failure on an OK receipt → 拒绝 (返回 False, quota 不动)
settled_guard = settle_failure(int(rctT["id"]), uidsT, uidT)  # rctT 已 ok
check("guarded: settle_failure on ok receipt → 拒绝, quota durable",
      settled_guard is False and _usage_n(uidT) == 1, f"settled={settled_guard}")
# guarded: finalize_ok on failed receipt → None (拒绝 terminal rewrite)
rctF2, uidsF2, _ = claim_ingress_quota(uidU, "127.0.0.1", f"unres2-{_u}", hU, [len(PNG)])
settle_failure(int(rctF2["id"]), uidsF2, uidU)
from repo.ingress_requests import finalize_ok as _ff, settle_failed as _sf
with db() as con:
    refused = _ff(con, int(rctF2["id"]), "[]", ts)
check("guarded: finalize_ok on failed receipt → None", refused is None)
# claim validation (runtime):
for bad_ref in (None, "", "   ", "x" * 129, 123):
    try:
        claim_ingress_quota(uidU, "127.0.0.1", bad_ref, hU, [len(PNG)])
        ok_bad = False
        break
    except ValueError:
        ok_bad = True
else:
    ok_bad = True
check("claim runtime ref validation (None/blank/long/non-str → ValueError)", ok_bad)
for bad_hash in (None, "abc", "Z" * 64, "a" * 63):
    try:
        claim_ingress_quota(uidU, "127.0.0.1", f"vh-{_u}", bad_hash, [len(PNG)])
        ok_bh = False
        break
    except ValueError:
        ok_bh = True
else:
    ok_bh = True
check("claim runtime hash validation (短/非hex/None → ValueError)", ok_bh)

# ══════════ F-series: fail-loud finalize + fail-closed replay decoder ══════════
from sharing.ingress_receipt import IngressUnresolved as _InUnres

# F1: failed receipt → service finalize_ok raises (not silent None)
uidF1 = _mkuser(_u + "f1")
hF1 = ingress_request_hash("v1_source",
                           [{"filename": "f1.bin", "content_type": "application/octet-stream",
                             "size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}],
                           {})
rctF1, uidsF1, _ = claim_ingress_quota(uidF1, "127.0.0.1", f"f1-{_u}", hF1, [len(PNG)])
settle_failure(int(rctF1["id"]), uidsF1, uidF1)
_f1_raised = False
try:
    finalize_ok(int(rctF1["id"]), [{"publicId": "NOPE"}])
except RuntimeError as _e:
    _f1_raised = "ingress_request_not_running" in str(_e)
check("F1 failed receipt → finalize_ok raises ingress_request_not_running",
      _f1_raised)

# F2: direct-share landing tx — receipt 非 running → 整事务回滚 (uploads=0)
uidF2 = _mkuser(_u + "f2")
hF2 = ingress_request_hash("web_direct_share",
                           [{"filename": "f2.png", "content_type": "image/png",
                             "size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}],
                           {"expiry_days": 3, "allowed_downloads": 10})
rctF2b, uidsF2b, _ = claim_ingress_quota(uidF2, "127.0.0.1", f"f2-{_u}", hF2, [len(PNG)])
# 先把 receipt 置 failed (模拟 recovery 抢先 settle), 再跑 direct-share landing 事务:
settle_failure(int(rctF2b["id"]), uidsF2b, uidF2)
from repo.uploads import insert_record as _ins2
from repo.ingress_requests import finalize_ok as _rfin2
_rolled = False
try:
    with db() as con:
        _ins2(con, uidF2, "127.0.0.1", "F2PID", "F2PID", "f2.png",
              "image/png", len(PNG), 3, 10, ts + 3 * 86400,
              "http://127.0.0.1:8113/f/F2PID", "", ts)
        _row_fin = _rfin2(con, int(rctF2b["id"]), "[]", ts)
        if _row_fin is None:
            raise RuntimeError("ingress_request_not_running")
except RuntimeError:
    _rolled = True
with db() as con:
    _f2_uploads = int(con.execute(
        "select count(*) c from uploads where user_id=%s", (uidF2,)).fetchone()["c"])
check("F2 direct-share: 非 running receipt → raise in-tx → uploads=0",
      _rolled and _f2_uploads == 0, f"rolled={_rolled} uploads={_f2_uploads}")

# F3: MCP UNRESOLVED — Gokapi staging 成功 + finalize 抛 + resolver UNRESOLVED
# 真实 uvicorn 是独立进程, 进程内 monkeypatch 不跨界 — 直接同进程调用
# route 函数 _call_tool (patch service 层生效), 证明 UNRESOLVED 分支行为。
import sharing.ingress_receipt as _irmod
_orig_fin, _orig_ro = _irmod.finalize_ok, _irmod.resolve_outcome
def _throw_fin(ingress_id, result):
    raise RuntimeError("simulated ack loss")
def _unres_resolve(ingress_id):
    return {"outcome": _irmod.OUTCOME_UNRESOLVED}
uidF3 = _mkuser(_u + "f3")
conn3 = _hc.HTTPConnection(*_HOST, timeout=30)
b_, ct_ = _form({"username": _u + "f3m", "password": "Test12345",
                 "password_confirm": "Test12345", "email": f"{_u}f3m@t.co"})
conn3.request("POST", "/auth/register", b_, {"Content-Type": ct_})
conn3.getresponse().read()
b_, ct_ = _form({"login": _u + "f3m", "password": "Test12345"})
conn3.request("POST", "/auth/login", b_, {"Content-Type": ct_})
r_ = conn3.getresponse(); r_.read()
ckF3 = (r_.getheader("Set-Cookie") or "").split(";")[0]
conn3.request("POST", "/api/tokens", json.dumps({"name": "f3"}).encode(),
              {"Cookie": ckF3, "Content-Type": "application/json"})
_bearerF3 = (json.loads(conn3.getresponse().read()).get("token") or {}).get("token")
conn3.close()
from economy.billing import grant as _g3
_g3(uidF3, 100000, "manual_topup", note="f3")
_gkF3_0 = _gk_count()
import asyncio as _aio
import app.routes.mcp as _mcpmod

class _FakeReqF3:
    headers = {"X-Real-IP": "127.0.0.1", "content-type": "application/json"}
    client = type("C", (), {"host": "127.0.0.1"})()

with db() as con:
    _uF3row = dict(con.execute("select * from users where id=%s",
                               (uidF3,)).fetchone())
_irmod.finalize_ok = _throw_fin
_irmod.resolve_outcome = _unres_resolve
try:
    _loop3 = _aio.new_event_loop()
    _rpc_res = _loop3.run_until_complete(_mcpmod._call_tool(
        _FakeReqF3(), _uF3row, 77,
        {"name": "compress",
         "arguments": {"files": [{"filename": "f3.png",
                                  "b64": base64.b64encode(PNG).decode()}],
                       "params": {},
                       "delivery": {"expiry_days": 3, "allowed_downloads": 10},
                       "client_ref": f"f3-{_u}-mcp"}}))
    _loop3.close()
finally:
    _irmod.finalize_ok = _orig_fin
    _irmod.resolve_outcome = _orig_ro
_errF3 = _rpc_res.get("error") or {}
with db() as con:
    _f3_receipt = con.execute(
        "select status from ingress_requests where user_id=%s and"
        " request_ref=%s", (uidF3, f"f3-{_u}-mcp")).fetchone()
    _f3_usage = int(con.execute(
        "select count(*) c from upload_usage where user_id=%s",
        (uidF3,)).fetchone()["c"])
check("F3 MCP UNRESOLVED: err_ingress_unresolved + zero delete + usage kept + receipt running",
      _errF3.get("message") == "err_ingress_unresolved"
      and _gk_count() == _gkF3_0 + 1  # staged object kept (zero delete)
      and _f3_usage == 1
      and _f3_receipt is not None and _f3_receipt["status"] == "running",
      f"err={_errF3} gk={_gkF3_0}->{_gk_count()} usage={_f3_usage} "
      f"st={_f3_receipt and _f3_receipt['status']}")

# F4: malformed replay — ok + '{bad' → claim raises IngressUnresolved
uidF4 = _mkuser(_u + "f4")
hF4 = ingress_request_hash("v1_source",
                           [{"filename": "f4.bin", "content_type": "application/octet-stream",
                             "size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}],
                           {})
rctF4, uidsF4, _ = claim_ingress_quota(uidF4, "127.0.0.1", f"f4-{_u}", hF4, [len(PNG)])
finalize_ok(int(rctF4["id"]), [{"publicId": "F4"}])
with db() as con:
    con.execute("update ingress_requests set result_json='{bad'"
                " where id=%s", (rctF4["id"],))
_f4_raised = False
try:
    claim_ingress_quota(uidF4, "127.0.0.1", f"f4-{_u}", hF4, [len(PNG)])
except _InUnres:
    _f4_raised = True
with db() as con:
    _f4_status = con.execute("select status from ingress_requests where id=%s",
                             (rctF4["id"],)).fetchone()["status"]
    _f4_usage = int(con.execute(
        "select count(*) c from upload_usage where user_id=%s", (uidF4,)).fetchone()["c"])
check("F4 malformed ok payload → IngressUnresolved + 零 mutation",
      _f4_raised and _f4_status == "ok" and _f4_usage == 1)

# F5: scalar replay — ok + '123'
with db() as con:
    con.execute("update ingress_requests set result_json='123'"
                " where id=%s", (rctF4["id"],))
_f5_raised = False
try:
    claim_ingress_quota(uidF4, "127.0.0.1", f"f4-{_u}", hF4, [len(PNG)])
except _InUnres:
    _f5_raised = True
check("F5 scalar ok payload → IngressUnresolved", _f5_raised)

# F6: resolve_outcome — ok + scalar → UNRESOLVED
outF6 = resolve_outcome(int(rctF4["id"]))
check("F6 resolve ok+scalar → OUTCOME_UNRESOLVED",
      outF6["outcome"] == OUTCOME_UNRESOLVED, str(outF6)[:80])

# ══════════ F7-F9: surface-level malformed durable-ok replay ══════════
# 一个可登录用户 + 三个 ok receipt (malformed/scalar payload), 三入口重试:
_ckS2 = _register(_u + "surf2")
with db() as con:
    uidW = con.execute("select id from users where username=%s",
                       (_u + "surf2",)).fetchone()["id"]
hS2 = ingress_request_hash("web_tool_source",
                           [{"filename": "s.png", "content_type": "image/png",
                             "size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}],
                           {"tool_id": "compress"})
refW = f"surf-{_u}-w"
rctW2, uidsW2, _ = claim_ingress_quota(uidW, "127.0.0.1", refW, hS2, [len(PNG)])
finalize_ok(int(rctW2["id"]), [{"publicId": "SURFW2"}])
with db() as con:
    con.execute("update ingress_requests set result_json='{bad'"
                " where id=%s", (rctW2["id"],))
_gkW0 = _gk_count()
_uuW0 = _usage_n(uidW)
body, ct = _multipart([("s.png", PNG, "image/png")],
                      {"tool": "compress", "client_ref": refW})
stW, rW = _req("POST", "/api/uploads", body, {"Content-Type": ct, "Cookie": _ckS2})
with db() as con:
    _stW = con.execute("select status from ingress_requests where id=%s",
                       (rctW2["id"],)).fetchone()["status"]
check("F7 Web malformed replay → 500 err_ingress_unresolved + 零副作用",
      stW == 500 and rW.get("error") == "err_ingress_unresolved"
      and _gk_count() == _gkW0 and _usage_n(uidW) == _uuW0
      and _stW == "ok",
      f"{stW} {str(rW)[:80]} gk={_gkW0}->{_gk_count()} usage={_uuW0}->{_usage_n(uidW)}")

# F8: v1 HTTP (同 receipt 形态, Idempotency-Key)
connS = _hc.HTTPConnection(*_HOST, timeout=30)
connS.request("POST", "/api/tokens", json.dumps({"name": "surf"}).encode(),
              {"Cookie": _ckS2, "Content-Type": "application/json"})
_bS = (json.loads(connS.getresponse().read()).get("token") or {}).get("token")
connS.close()
hV1 = ingress_request_hash("v1_source",
                            [{"filename": "s.png", "content_type": "image/png",
                              "size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}],
                            {})
refV = f"surf-{_u}-v"
rctV, uidsV, _ = claim_ingress_quota(uidW, "127.0.0.1", refV, hV1, [len(PNG)])
finalize_ok(int(rctV["id"]), [{"publicId": "SURFV"}])
with db() as con:
    con.execute("update ingress_requests set result_json='123'"
                " where id=%s", (rctV["id"],))
_gkV0 = _gk_count()
_uuV0 = _usage_n(uidW)
stV, rV = _req("POST", "/api/v1/uploads", PNG, {
    "Content-Type": "image/png", "x-filename": "s.png",
    "Content-Length": str(len(PNG)),
    "Authorization": f"Bearer {_bS}",
    "Idempotency-Key": refV})
with db() as con:
    _stV = con.execute("select status from ingress_requests where id=%s",
                       (rctV["id"],)).fetchone()["status"]
check("F8 v1 scalar replay → 500 err_ingress_unresolved + 零副作用",
      stV == 500 and rV.get("error") == "err_ingress_unresolved"
      and _gk_count() == _gkV0 and _usage_n(uidW) == _uuV0
      and _stV == "ok",
      f"{stV} {str(rV)[:80]} gk={_gkV0}->{_gk_count()} usage={_uuV0}->{_usage_n(uidW)}")

# F9: MCP JSON-RPC (同 receipt 形态, client_ref)
hMCP = ingress_request_hash("mcp_tool_source",
                             [{"filename": "s.png", "content_type": "image/png",
                               "size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}],
                             {"tool_id": "compress"})
refM = f"surf-{_u}-m"
rctM, uidsM, _ = claim_ingress_quota(uidW, "127.0.0.1", refM, hMCP, [len(PNG)])
finalize_ok(int(rctM["id"]), [{"publicId": "SURFM"}])
with db() as con:
    con.execute("update ingress_requests set result_json='[1,2]'"
                " where id=%s", (rctM["id"],))  # list 合法 — 改 scalar 破坏:
    con.execute("update ingress_requests set result_json='42'"
                " where id=%s", (rctM["id"],))
_gkM0 = _gk_count()
_uuM0 = _usage_n(uidW)
def _mcpS(arguments, name="compress", mid=99):
    b_ = {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
          "params": {"name": name, "arguments": arguments}}
    st_, r_ = _req("POST", "/mcp", b_, {
        "Content-Type": "application/json", "Authorization": f"Bearer {_bS}",
        "Accept": "application/json, text/event-stream"})
    if isinstance(r_, str):
        for _ln in r_.split("\n"):
            if _ln.startswith("data:"):
                r_ = json.loads(_ln[5:]); break
        else:
            r_ = json.loads(r_)
    return st_, r_
stM, rM = _mcpS({"files": [{"filename": "s.png",
                            "b64": base64.b64encode(PNG).decode()}],
                 "params": {}, "delivery": {"expiry_days": 3,
                                            "allowed_downloads": 10},
                 "client_ref": refM})
with db() as con:
    _stM = con.execute("select status from ingress_requests where id=%s",
                       (rctM["id"],)).fetchone()["status"]
_errM = rM.get("error") or {}
check("F9 MCP scalar replay → -32000 err_ingress_unresolved + 零副作用",
      _errM.get("code") == -32000
      and _errM.get("message") == "err_ingress_unresolved"
      and _gk_count() == _gkM0 and _usage_n(uidW) == _uuM0
      and _stM == "ok",
      f"err={_errM} gk={_gkM0}->{_gk_count()} usage={_uuM0}->{_usage_n(uidW)}")

# ══════════ F10/F11: empty-payload durable ok — decoder is sole authority ══════════
uidF10 = _mkuser(_u + "f10")
hF10 = ingress_request_hash("v1_source",
                            [{"filename": "f10.bin", "content_type": "application/octet-stream",
                              "size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}],
                            {})
# F10: earliest ok + result_json='' → IngressUnresolved
rct10, uids10, _ = claim_ingress_quota(uidF10, "127.0.0.1", f"f10-{_u}", hF10, [len(PNG)])
finalize_ok(int(rct10["id"]), [{"publicId": "F10"}])
with db() as con:
    con.execute("update ingress_requests set result_json='' where id=%s",
                (rct10["id"],))
_f10_raised = False
try:
    claim_ingress_quota(uidF10, "127.0.0.1", f"f10-{_u}", hF10, [len(PNG)])
except _InUnres:
    _f10_raised = True
with db() as con:
    _f10_status = con.execute(
        "select status from ingress_requests where id=%s",
        (rct10["id"],)).fetchone()["status"]
check("F10 earliest ok + empty payload → IngressUnresolved + 零新 usage + receipt 仍 ok",
      _f10_raised and _usage_n(uidF10) == 1 and _f10_status == "ok",
      f"raised={_f10_raised} usage={_usage_n(uidF10)} st={_f10_status}")

# F11: failed history + later live ok(result_json='') → IngressUnresolved
uidF11 = _mkuser(_u + "f11")
rct11a, uids11a, _ = claim_ingress_quota(uidF11, "127.0.0.1", f"f11-{_u}",
                                         hF10, [len(PNG)])
settle_failure(int(rct11a["id"]), uids11a, uidF11)   # failed history
rct11b, uids11b, _ = claim_ingress_quota(uidF11, "127.0.0.1", f"f11-{_u}",
                                         hF10, [len(PNG)])  # same hash retry
finalize_ok(int(rct11b["id"]), [{"publicId": "F11"}])
with db() as con:
    con.execute("update ingress_requests set result_json='' where id=%s",
                (rct11b["id"],))
_f11_raised = False
try:
    claim_ingress_quota(uidF11, "127.0.0.1", f"f11-{_u}", hF10, [len(PNG)])
except _InUnres:
    _f11_raised = True
with db() as con:
    _f11_live = con.execute(
        "select status from ingress_requests where id=%s",
        (rct11b["id"],)).fetchone()["status"]
check("F11 failed→ok + later live ok empty payload → IngressUnresolved + 零新 usage + 仍 ok",
      _f11_raised and _usage_n(uidF11) == 1 and _f11_live == "ok",
      f"raised={_f11_raised} usage={_usage_n(uidF11)} st={_f11_live}")

# ══════════ 8-10. receipt states (直接 service 层 fault-injection) ══════════
from sharing.ingress_receipt import recover_abandoned_ingress_requests

uidS = _mkuser(_u + "svc")
hA = ingress_request_hash("web_tool_source",
                          [{"filename": "a.png", "content_type": "image/png",
                            "size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}],
                          {"tool_id": "compress"})
hB = ingress_request_hash("web_tool_source",
                          [{"filename": "b.png", "content_type": "image/png",
                            "size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}],
                          {"tool_id": "compress"})
refS = f"svc-{_u}-1"
rct, uids, replay = claim_ingress_quota(uidS, "127.0.0.1", refS, hA, [len(PNG)])
check("G claim ok → running receipt + usage", rct["status"] == "running" and len(uids) == 1)

# 8: running same hash → in_progress
try:
    claim_ingress_quota(uidS, "127.0.0.1", refS, hA, [len(PNG)])
    check("8 running same hash → in_progress", False, "no raise")
except IngressInProgress:
    check("8 running same hash → in_progress", True)

# 12: finalize rollback 模拟 (settle failure path)
settle_failure(int(rct["id"]), uids, uidS)
check("11/12 failed settle → usage released + receipt failed",
      _usage_n(uidS) == 0)
with db() as con:
    rs = con.execute("select status from ingress_requests where id=%s",
                     (rct["id"],)).fetchone()
    check("12b receipt terminal failed", rs["status"] == "failed")

# 9: failed same hash → new attempt
rct2, uids2, _ = claim_ingress_quota(uidS, "127.0.0.1", refS, hA, [len(PNG)])
check("9 failed same hash → new attempt allowed", rct2["id"] != rct["id"])

# 10: failed different hash → conflict
try:
    claim_ingress_quota(uidS, "127.0.0.1", refS, hB, [len(PNG)])
    check("10 failed changed hash → conflict", False, "no raise")
except IngressConflict:
    check("10 failed changed hash → conflict", True)

# 13: commit-ACK durable ok → replay no cleanup
finalize_ok(int(rct2["id"]), [{"publicId": "X1"}])
out = resolve_outcome(int(rct2["id"]))
check("13 outcome durable_ok + stored result",
      out["outcome"] == OUTCOME_DURABLE_OK and out["result"][0]["publicId"] == "X1")
# earliest=failed + later=ok: claim must REPLAY the live ok receipt
rct3, uids3, replay3 = claim_ingress_quota(uidS, "127.0.0.1", refS, hA, [len(PNG)])
check("13b ok receipt same hash claim → replay payload (failed-then-ok history)",
      rct3 is None and replay3 == [{"publicId": "X1"}],
      f"rct={rct3} replay={replay3}")

# 14: outcome DB unavailable → unresolved (构造 malformed ok)
with db() as con:
    con.execute("update ingress_requests set result_json='{bad json'"
                " where id=%s", (rct2["id"],))
out = resolve_outcome(int(rct2["id"]))
check("14 malformed ok payload → UNRESOLVED (no release/delete)",
      out["outcome"] == OUTCOME_UNRESOLVED)

# 15-16. startup recovery
uidR = _mkuser(_u + "rec")
rctR, uidsR, _ = claim_ingress_quota(uidR, "127.0.0.1", f"rec-{_u}", hA, [len(PNG)])
check("15a running receipt fixture", _usage_n(uidR) == 1
      and rctR["status"] == "running")
gk_before = _gk_count()
stats = recover_abandoned_ingress_requests()
with db() as con:
    rr = con.execute("select status from ingress_requests where id=%s",
                     (rctR["id"],)).fetchone()
check("15 startup recovery → quota released + failed",
      _usage_n(uidR) == 0 and rr["status"] == "failed" and stats["recovered"] >= 1,
      f"usage={_usage_n(uidR)} stats={stats}")
check("16 recovery zero Gokapi delete attempts", _gk_count() == gk_before)

# 30-31. concurrency
import threading
uidC = _mkuser(_u + "conc")
refC = f"conc-{_u}"
resC = []
bar = threading.Barrier(2)
def _w(h, tag):
    bar.wait()
    try:
        r, u, _rp = claim_ingress_quota(uidC, "127.0.0.1", refC, h, [len(PNG)])
        resC.append(("ok", r["id"]))
    except (IngressConflict, IngressInProgress) as e:
        resC.append((type(e).__name__, None))
t1 = threading.Thread(target=_w, args=(hA, "a"))
t2 = threading.Thread(target=_w, args=(hB, "b"))
t1.start(); t2.start(); t1.join(); t2.join()
with db() as con:
    distinct = con.execute(
        "select count(distinct request_hash) c from ingress_requests"
        " where user_id=%s and request_ref=%s", (uidC, refC)).fetchone()["c"]
ok_n = sum(1 for k, _ in resC if k == "ok")
check("30/31 concurrent same ref different hash → 1 owner, 1 binding",
      ok_n == 1 and distinct == 1, f"res={resC} distinct={distinct}")

# ══════════ O-complement: tool ref permanent binding race ══════════
from repo.ledger import claim_call
from governance.commission import commission_rate
from execution.invocation import invocation_fingerprint, ToolInvocation, SourceFile
uidO = _mkuser(_u + "orcef")
tO = f"igtool_{_u}"
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " tool_type, label_json, icon, multi, pass_through, visibility,"
        " enabled, created_at, updated_at) values (%s, NULL, %s, 'local',"
        " 'produce', %s, 'wand', FALSE, FALSE, 'public', TRUE, %s, %s)",
        (tO, tO, json.dumps({"en": "IG"}), ts, ts))
    con.execute("insert into pricing (tool_id, cost, enabled, updated_at)"
                " values (%s, 0, TRUE, %s)", (tO, ts))
rate = commission_rate()
invA = ToolInvocation(tO, [SourceFile("pA", "")], {},
                      {"expiry_days": 7, "allowed_downloads": 5}, f"race-{_u}")
hashA = invocation_fingerprint(invA)
hashB2 = invocation_fingerprint(ToolInvocation(
    tO, [SourceFile("pA", "")], {"preset": "other"},
    {"expiry_days": 7, "allowed_downloads": 5}, f"race-{_u}"))

# race window: B reads "no history" → pauses; A claims hashA and even
# reaches failed; B resumes. The user-row FOR UPDATE lock serializes the
# two claim transactions: B's history read happens AFTER A commits, so
# B sees A's binding. Deterministic sequencing (single connection per
# phase, no real pause needed — the lock already orders them):
with db() as con:
    a_row, a_cl = claim_call(con, f"race-{_u}", uidO, tO, 0, "{}", ts,
                             commission_rate_snapshot=rate,
                             invocation_hash=hashA)
    con.execute("update tool_calls set status='failed', finished_at=%s"
                " where id=%s", (ts, a_row["id"]))
b_claimed = None
with db() as con:
    _, b_claimed = claim_call(con, f"race-{_u}", uidO, tO, 0, "{}", ts,
                              commission_rate_snapshot=rate,
                              invocation_hash=hashB2)
with db() as con:
    distinct_hashes = con.execute(
        "select count(distinct invocation_hash) c from tool_calls"
        " where user_id=%s and tool_id=%s and client_ref=%s",
        (uidO, tO, f"race-{_u}")).fetchone()["c"]
check("A2 tool ref permanent binding: A failed 后 B different hash → 仍 conflict",
      b_claimed is False and distinct_hashes == 1,
      f"claimed={b_claimed} distinct={distinct_hashes}")

# ══════════ cleanup ══════════
with db() as con:
    _http_uids = [r["id"] for r in con.execute(
        "select id from users where username like %s",
        (f"{_u}%",)).fetchall()]
_uids_all = [uid1, uid2, uid3, uidS, uidR, uidC, uidO, uidT, uidD2, uidU,
              uidF1, uidF2, uidF3, uidF4, uidW, uidF10, uidF11] + _http_uids
with db() as con:
    for uid in _uids_all:
        con.execute("delete from operator_audit where actor_user_id=%s", (uid,))
for uid in _uids_all:
    with db() as con:
        con.execute("delete from tool_calls where user_id=%s", (uid,))
        con.execute("delete from uploads where user_id=%s", (uid,))
        con.execute("delete from ledger where user_id=%s", (uid,))
with db() as con:
    con.execute("delete from pricing where tool_id=%s", (tO,))
    con.execute("delete from platform_tools where tool_key=%s", (tO,))
for uid in _uids_all:
    with db() as con:
        con.execute("delete from users where id=%s", (uid,))
print("cleanup: ingress fixtures removed")

print()
print(f"FAIL: {' — '.join(FAIL) if FAIL else '无'} — ({COUNT} checks)")
_sys.exit(1 if FAIL else 0)
