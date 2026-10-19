#!/usr/bin/env python3
"""C2b back-office completion canonical.

  T*  Tool supply list: search (key / label / owner), class filter
      (official vs community), delisted filter, pagination.
  D*  Tool detail page: declaration (labels per language, owner, http
      endpoint WITHOUT credentials, input window, outputs, params,
      pricing), 30-day call stats + provider earn, recent calls with
      user links, this tool's audit history; mutations reuse the same
      audited endpoints (delist switch shown).
  C*  Content governance: files (uploads) and results (/r) found by
      share id / result id / user / tool; takedown deletes the mapping
      row via the sharing single point and writes one upload.delete
      audit fact (snapshot carries identity fields only).
  L*  Call log: filters (tool / user / status / error / date range),
      per-row charge + settlement derived from ledger facts
      (provider earn + commission / platform revenue / refunded).
  O*  Overview daily table: 14/30-day windows, per-day registrations /
      calls / fail rate / net credits charged / platform income /
      provider earn — aggregated live from users/tool_calls/ledger and
      equal to direct SQL on today's row.
  K*  User detail API keys: prefix-only listing (full key never
      rendered), operator delete removes the row and writes one
      api_key.delete audit fact (no hash/sealed material); replay 404.
  X*  Operator gate on every new surface: browser session of a normal
      user → login redirect; non-browser → 401 JSON; POST endpoints
      denied the same way.
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
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:130]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import json
import http.client as _hc
import time
import urllib.parse as _up
ts = int(time.time())
_u = "c2b" + _os.urandom(4).hex()

from database import db

HOST = ("127.0.0.1", int(_TEST_PORT))


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*HOST, timeout=60)
    payload = None
    if body is not None:
        payload = body if isinstance(body, (str, bytes)) else json.dumps(body).encode()
        if isinstance(payload, str):
            payload = payload.encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    try:
        return r.status, hdrs, json.loads(data) if data else None
    except Exception:
        return r.status, hdrs, data.decode(errors="replace")


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _register(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    conn = _hc.HTTPConnection(*HOST, timeout=30)
    b, ct = _form({"login": name, "password": "Test12345"})
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close()
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck


op_name = _u + "op"
op, OP_CK = _register(op_name)
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (op,))

prov_name = _u + "prov"
prov, PROV_CK = _register(prov_name)
caller_name = _u + "caller"
caller, CALLER_CK = _register(caller_name)

TOOL = f"{prov_name}/paytool"
AUTH_TOKEN_SECRET = "sk-c2b-secret-token-value"

# one provider tool (metered, params, auth) + one official local row
with db() as con:
    con.execute(
        """
        INSERT INTO platform_tools (tool_key, tool_id, execution,
          visibility, label_json, desc_json, enabled, owner_id,
          files_min, files_max, accept_json, format_family, outputs_json, params_json,
          endpoint_json, featured, sort_order, created_at, updated_at)
        VALUES (%s, 'paytool', 'http', 'public',
          '{"zh":"付费工具","en":"Paid Tool"}',
          '{"zh":"说明文字","en":"desc words"}', TRUE, %s,
          1, 3, '[".png",".pdf"]', 'image', '["text","file"]',
          '[{"key":"mode","type":"enum","label":"Mode",
             "required":true,"options":[{"value":"fast"}]}]',
          %s, TRUE, 5, %s, %s)
        """,
        (TOOL, prov, json.dumps(
            {"url": "https://c2b.example/hook", "timeout_s": 45,
             "auth_header": "X-Key", "auth_token": AUTH_TOKEN_SECRET}),
         ts, ts))
    con.execute(
        "INSERT INTO pricing (tool_id, cost, enabled, updated_at)"
        " VALUES (%s, 30, TRUE, %s)", (TOOL, ts))
    con.execute(
        """
        INSERT INTO platform_tools (tool_key, tool_id, execution,
          visibility, label_json, enabled, created_at, updated_at, accept_json, format_family)
        VALUES ('c2boff', 'c2boff', 'local', 'public',
          '{"zh":"官方工具","en":"Official"}', TRUE, %s, %s, '[\".png\"]', 'image')
        """, (ts, ts))
    con.execute(
        "INSERT INTO pricing (tool_id, cost, enabled, updated_at)"
        " VALUES ('c2boff', 0, TRUE, %s)", (ts,))

# calls + settlement ledger facts: one ok metered third-party call
# (payout 21 + commission 9 millis), one failed refunded call, one
# insufficient attempt — plus today's registration facts (3 users above)
with db() as con:
    pid = con.execute("select id from users where username='platform'"
                      ).fetchone()["id"]
    con.execute(
        """
        INSERT INTO tool_calls (client_ref, user_id, tool_id, status,
          cost_snapshot, error, started_at, finished_at,
          commission_rate_snapshot, invocation_hash, result_public_id,
          result_json)
        VALUES ('c2b-ok', %s, %s, 'ok', 30, NULL, %s, %s, 30, 'h1',
                'rsltc2bok',
                '{"result_id":"rsltc2bok","items":[{"kind":"file",
                  "public_id":"shrc2bone","filename":"report.pdf"}]}')
        """, (caller, TOOL, ts, ts))
    ok_call = con.execute(
        "select id from tool_calls where client_ref='c2b-ok'"
        ).fetchone()["id"]
    con.execute(
        """
        INSERT INTO tool_calls (client_ref, user_id, tool_id, status,
          cost_snapshot, error, started_at, finished_at,
          commission_rate_snapshot, invocation_hash)
        VALUES ('c2b-fail', %s, %s, 'failed', 30, 'tool_failed', %s, %s,
                30, 'h2')
        """, (caller, TOOL, ts, ts))
    fail_call = con.execute(
        "select id from tool_calls where client_ref='c2b-fail'"
        ).fetchone()["id"]
    con.execute(
        """
        INSERT INTO tool_calls (client_ref, user_id, tool_id, status,
          cost_snapshot, error, started_at, finished_at,
          commission_rate_snapshot, invocation_hash)
        VALUES ('c2b-ins', %s, 'c2boff', 'insufficient', 0,
                'balance:0', %s, %s, 0, 'h3')
        """, (caller, ts, ts))
    for row in (
        (caller, -30, 70, "consume", "tool_call", str(ok_call),
         f"call:{ok_call}", TOOL, ts),
        (prov, 21, 21, "provider_earn", "tool_call", str(ok_call),
         f"payout:{ok_call}", "payout", ts),
        (pid, 9, 9, "platform_commission", "tool_call", str(ok_call),
         f"platform_commission:{ok_call}", "commission", ts),
        (caller, -30, 40, "consume", "tool_call", str(fail_call),
         f"call:{fail_call}", TOOL, ts),
        (caller, 30, 70, "refund", "ledger", None, None, "refund", ts),
    ):
        con.execute(
            """
            INSERT INTO ledger (user_id, delta, balance_after, kind,
              ref_type, ref_id, idem_key, note, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, row)
    charge = con.execute(
        "select id from ledger where kind='consume' and idem_key=%s",
        (f"call:{fail_call}",)).fetchone()["id"]
    refund_id = con.execute(
        "select id from ledger where kind='refund' and note='refund'"
        " order by id desc limit 1").fetchone()["id"]
    con.execute("update ledger set ref_id=%s where id=%s",
                (str(charge), refund_id))
    con.execute(
        """
        INSERT INTO uploads (user_id, ip, gokapi_id, public_id,
          file_name, content_type, size_bytes, expiry_days,
          allowed_downloads, expire_at, url_download, url_hotlink,
          created_at)
        VALUES (%s, '127.0.0.1', 'gkc2bfake', 'shrc2bone',
          'report.pdf', 'application/pdf', 2048, 7, 5, %s,
          '/f/shrc2bone', NULL, %s)
        """, (caller, ts + 7 * 86400, ts))

# ═══ T. Tool supply list: search / filters / pagination ═══
H = {"Cookie": OP_CK}
st, _h, body = _req("GET", "/samelabs/tools", None, H)
body = body or ""
check("T1 default list renders both rows",
      TOOL in body and "c2boff" in body, st)
st, _h, body = _req("GET", f"/samelabs/tools?q={_up.quote('付费工具')}",
                    None, H)
check("T2 search matches label language (zh)",
      TOOL in (body or "") and "c2boff" not in (body or ""), st)
st, _h, body = _req("GET", f"/samelabs/tools?q={prov_name}", None, H)
check("T3 search matches owner username",
      TOOL in (body or ""), st)
st, _h, body = _req("GET", "/samelabs/tools?cls=official", None, H)
check("T4 official filter hides community rows",
      "c2boff" in (body or "") and TOOL not in (body or ""), st)
st, _h, body = _req("GET", "/samelabs/tools?cls=community", None, H)
check("T5 community filter hides official rows",
      TOOL in (body or "") and "c2boff" not in (body or ""), st)
st, _h, body = _req("GET", "/samelabs/tools?featured=1", None, H)
check("T6 featured filter keeps only featured",
      TOOL in (body or "") and "c2boff" not in (body or ""), st)
st, _h, body = _req("GET",
                    "/samelabs/tools?q=" + _up.quote("不存在的词xyz"),
                    None, H)
check("T7 no-hit search shows the empty state",
      "暂无工具" in (body or ""), st)
st, _h, body = _req("GET", "/samelabs/tools?q=&page=99", None, H)
check("T8 far page: valid response + empty state",
      st == 200 and "暂无工具" in (body or ""), st)

# ═══ D. Tool detail page ═══
enc = _up.quote(TOOL, safe="")
st, _h, body = _req("GET", f"/samelabs/tools/{enc}", None, H)
b = body or ""
check("D1 detail renders declaration",
      st == 200 and "付费工具" in b and "Paid Tool" in b
      and "说明文字" in b and "desc words" in b, st)
check("D2 owner links to the user detail page",
      f'href="/samelabs/users/{prov}"' in b)
check("D3 endpoint URL + timeout shown, credential NEVER",
      "https://c2b.example/hook" in b and "45" in b
      and AUTH_TOKEN_SECRET not in b and "X-Key" not in b)
check("D4 input window / formats / outputs / params",
      "1 – 3" in b and ".png" in b and ".pdf" in b
      and "text / file" in b and "mode" in b and "enum" in b)
check("D5 pricing + 30-day stats + earn",
      "30" in b and "已配置" in b
      and "$0.021" in b)
check("D6 recent call links user + result page",
      f'href="/samelabs/users/{caller}"' in b and "/r/rsltc2bok" in b)
check("D7 unknown tool detail → 404",
      _req("GET", "/samelabs/tools/nobody%2Fnone", None, H)[0] == 404)
# audit history reflects a real audited mutation (delist via endpoint)
st, _h, d = _req("POST", f"/samelabs/tools/{enc}/delist",
                 {"delisted": True},
                 {"Content-Type": "application/json", "Cookie": OP_CK})
st, _h, body = _req("GET", f"/samelabs/tools/{enc}", None, H)
check("D8 detail audit history shows the delist fact",
      d.get("ok") and "tool.delisted.update" in (body or ""),
      (st, d))
st, _h, body = _req("GET", "/samelabs/tools?delisted=1", None, H)
check("T9 delisted filter finds the taken-down tool",
      TOOL in (body or ""))
_req("POST", f"/samelabs/tools/{enc}/delist", {"delisted": False},
     {"Content-Type": "application/json", "Cookie": OP_CK})

# ═══ C. Content governance ═══
st, _h, body = _req("GET", "/samelabs/content?share=shrc2bone", None, H)
b = body or ""
check("C1 find file by share id",
      st == 200 and "report.pdf" in b and "application/pdf" in b
      and "2.0 KB" in b and f"/samelabs/users/{caller}" in b
      and "/f/shrc2bone" in b, st)
st, _h, body = _req("GET", f"/samelabs/content?user={caller_name}",
                    None, H)
check("C2 file list by username + result list by user",
      "report.pdf" in (body or "") and "/r/rsltc2bok" in (body or ""))
st, _h, body = _req("GET", f"/samelabs/content?tool={_up.quote(TOOL)}",
                    None, H)
check("C3 tool filter finds the produced file + the result",
      "report.pdf" in (body or "") and "/r/rsltc2bok" in (body or ""))
st, _h, body = _req("GET",
                    "/samelabs/content?share=missing&result=missing",
                    None, H)
check("C4 no-hit shows empty states",
      "未找到文件" in (body or "") and "未找到结果" in (body or ""))
st, _h, d = _req("POST", "/samelabs/content/uploads/999999/delete",
                 {}, {"Content-Type": "application/json", "Cookie": OP_CK})
check("C5 takedown of unknown upload → 404", st == 404, st)
with db() as con:
    up_id = con.execute(
        "select id from uploads where public_id='shrc2bone'"
        ).fetchone()["id"]
st, _h, d = _req("POST", f"/samelabs/content/uploads/{up_id}/delete",
                 {}, {"Content-Type": "application/json", "Cookie": OP_CK})
with db() as con:
    gone = con.execute(
        "select 1 from uploads where id=%s", (up_id,)).fetchone()
    au = con.execute(
        "select before_json, after_json from operator_audit"
        " where action='upload.delete' and target_type='upload'"
        " and target_id=%s", (str(up_id),)).fetchone()
check("C6 takedown deletes the mapping row + audits",
      d.get("ok") and gone is None and au is not None
      and au["before_json"]["public_id"] == "shrc2bone"
      and au["before_json"]["file_name"] == "report.pdf"
      and au["after_json"] is None, (d, au and dict(au)))
check("C7 audit snapshot carries identity fields only",
      set(au["before_json"].keys()) ==
      {"id", "user_id", "public_id", "file_name", "content_type",
       "size_bytes"}, sorted(au["before_json"].keys()))

# ═══ L. Call log ═══
st, _h, body = _req("GET", "/samelabs/calls", None, H)
b = body or ""
# Jinja |urlencode keeps '/' readable in the href — the :path route
# accepts the raw key, so assert the unencoded link
check("L1 default log renders both calls + links",
      f'href="/samelabs/tools/{TOOL}"' in b
      and f'href="/samelabs/users/{caller}"' in b, st)
check("L2 ok row shows charge + payout/commission settlement",
      "30" in b and "开发者收益 $0.021" in b and "抽佣 $0.009" in b)
check("L3 failed row shows refunded",
      "已退款" in b and "tool_failed" in b)
st, _h, body = _req("GET", "/samelabs/calls?status=failed", None, H)
b = body or ""
check("L4 status filter",
      "tool_failed" in b and "/r/rsltc2bok" not in b)
st, _h, body = _req("GET",
                    f"/samelabs/calls?tool={_up.quote(TOOL)}&error=tool",
                    None, H)
b = body or ""
check("L5 tool + error filters combine",
      "tool_failed" in b and "rsltc2bok" not in b)
_today = time.strftime("%Y-%m-%d", time.gmtime())
st, _h, body = _req("GET",
                    f"/samelabs/calls?user={caller_name}"
                    f"&date_from={_today}&date_to={_today}", None, H)
check("L6 user + date range filters find today's rows",
      "tool_failed" in (body or ""))
st, _h, body = _req("GET",
                    f"/samelabs/calls?user={caller_name}&date_from=2001-01-01&date_to=2001-01-02",
                    None, H)
check("L7 out-of-range dates → empty",
      "暂无调用" in (body or ""))

# ═══ O. Overview daily table ═══
st, _h, body = _req("GET", "/samelabs?range=14", None, H)
b = body or ""
_oldest = time.strftime("%Y-%m-%d", time.gmtime(ts - 13 * 86400))
check("O1 daily table renders the full 14-day window",
      "最近 14 天" in b and f"<td>{_oldest}</td>" in b, st)
with db() as con:
    direct = con.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM users
             WHERE (to_timestamp(created_at) AT TIME ZONE 'UTC')::date
               = (to_timestamp(%s) AT TIME ZONE 'UTC')::date) AS reg,
          (SELECT COUNT(*) FROM tool_calls
             WHERE (to_timestamp(started_at) AT TIME ZONE 'UTC')::date
               = (to_timestamp(%s) AT TIME ZONE 'UTC')::date) AS calls,
          (SELECT -COALESCE(SUM(delta),0) FROM ledger WHERE kind='consume'
             AND (to_timestamp(created_at) AT TIME ZONE 'UTC')::date
               = (to_timestamp(%s) AT TIME ZONE 'UTC')::date)
           - (SELECT COALESCE(SUM(delta),0) FROM ledger WHERE kind='refund'
             AND (to_timestamp(created_at) AT TIME ZONE 'UTC')::date
               = (to_timestamp(%s) AT TIME ZONE 'UTC')::date) AS net
        """, (ts, ts, ts, ts)).fetchone()
_day = time.strftime("%Y-%m-%d", time.gmtime(ts))
check("O2 today's row equals direct SQL (registrations/calls/net)",
      f"<td>{_day}</td>" in b
      and f">{int(direct['reg']):,}<" in b
      and f">{int(direct['calls']):,}<" in b
      and f">{int(direct['net']):,}<" in b,
      (dict(direct), _day))
st, _h, body30 = _req("GET", "/samelabs?range=30", None, H)
check("O3 30-day window available",
      "最近 30 天" in (body30 or ""))
st, _h, body9 = _req("GET", "/samelabs?range=9", None, H)
check("O4 out-of-vocabulary range falls back to 30",
      "最近 30 天" in (body9 or ""))

# ═══ K. User detail: API keys ═══
from identity.apikeys import create_key
krow = create_key(caller, "ci key")
full_key = krow["key"]
st, _h, body = _req("GET", f"/samelabs/users/{caller}", None, H)
b = body or ""
check("K1 user detail lists files + key prefix only",
      "ci key" in b and (krow["prefix"] + "…") in b
      and full_key not in b, st)
st, _h, d = _req("POST",
                 f"/samelabs/users/{caller}/keys/{krow['id']}/delete",
                 {}, {"Content-Type": "application/json", "Cookie": OP_CK})
with db() as con:
    gone = con.execute("select 1 from api_keys where id=%s",
                       (krow["id"],)).fetchone()
    au = con.execute(
        "select before_json, after_json from operator_audit"
        " where action='api_key.delete' and target_type='api_key'"
        " and target_id=%s", (str(krow["id"]),)).fetchone()
check("K2 operator key delete removes row + audits (prefix only)",
      d.get("ok") and gone is None and au is not None
      and au["before_json"]["prefix"] == krow["prefix"]
      and au["after_json"] is None, (d, au and dict(au)))
check("K3 audit snapshot has no secret material",
      set(au["before_json"].keys()) == {"id", "user_id", "name",
                                        "prefix"})
st, _h, d = _req("POST",
                 f"/samelabs/users/{caller}/keys/{krow['id']}/delete",
                 {}, {"Content-Type": "application/json", "Cookie": OP_CK})
check("K4 replay delete → 404", st == 404, st)
# authentication really stops for a deleted key (identity single point)
st, _h, d = _req("GET", "/api/me", None,
                 {"Authorization": f"Bearer {full_key}"})
check("K5 deleted key no longer authenticates", st == 401, st)

# ═══ X. Operator gate on the new surfaces ═══
for path, redir in (("/samelabs/content", True), ("/samelabs/calls", True),
                    (f"/samelabs/tools/{enc}", True),
                    (f"/samelabs/users/{caller}/keys/1/delete", False)):
    st, hdrs, _b = _req("GET" if redir else "POST", path, None,
                        {"Cookie": CALLER_CK,
                         "Accept": "text/html"} if redir else
                        {"Cookie": CALLER_CK,
                         "Content-Type": "application/json"})
    if redir:
        check(f"X {path} normal session → login redirect",
              st == 302 and "/auth/login" in hdrs.get("location", ""),
              (st, hdrs.get("location")))
    else:
        check(f"X {path} normal session POST → 401 JSON",
              st == 401, st)
st, hdrs, _b = _req("GET", "/samelabs/content")
check("X anonymous non-browser → 401 JSON", st == 401, st)

with db() as con:
    purge_users(con, [op, prov, caller])
# tool rows / calls / ledger facts of this run roll away with the users;
# the one platform-side commission row is removed by its idem key (the
# platform bookkeeping account itself is never touched)
with db() as con:
    con.execute("delete from ledger where idem_key=%s",
                (f"platform_commission:{ok_call}",))
    con.execute("delete from pricing where tool_id=%s", (TOOL,))
    con.execute("delete from platform_tools where tool_key in (%s,%s)",
                (TOOL, 'c2boff'))

print()
print("FAIL:", FAIL if FAIL else "无 — 全绿")
_sys.exit(1 if FAIL else 0)
