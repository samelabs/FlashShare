"""Body-tool contract: declaration storage, IO-1 source.body, and a
temporarily registered local body tool executed in a REAL worker
subprocess (the child re-runs load_all and must discover it).

P1 of the tool-api batch: 文件工具与正文工具是两个互斥输入方式 —
files_min=files_max=0 + body_format ∈ {text,json} (+可选 placeholder)。
"""
import os as _os
import sys as _sys

_TEST_PORT = _os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT or _TEST_PORT == "8000":
    _sys.stderr.write("ERROR: isolated FLASHSHARE_TEST_PORT required.\n"); _sys.exit(2)
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
_os.environ.setdefault("FLASHSHARE_GOKAPI_URL", "http://127.0.0.1:53901")
_os.environ.setdefault("FLASHSHARE_GOKAPI_KEY", "test-gk-key")
from tests.support.cleanup import purge_users  # noqa: E402

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
import subprocess
import time
import uuid
ts = int(time.time())
_u = "bt" + _os.urandom(4).hex()
_HOST = ("127.0.0.1", int(_TEST_PORT))

from database import db  # noqa: E402


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=60)
    if body is None:
        payload = None
    elif isinstance(body, bytes):
        payload = body
    else:
        payload = json.dumps(body).encode()
    conn.request(method, path, payload, headers or {})
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    try:
        return r.status, json.loads(raw) if raw else {}
    except Exception:
        return r.status, {}


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _register_login(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{name}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": name, "password": "Test12345"})
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close()
    return ck


ck = _register_login(_u)
with db() as con:
    uid = con.execute("select id from users where username=%s", (_u,)).fetchone()["id"]

# ═══ A. declaration + storage + projection (user supply) ═══
st, r = _req("POST", "/api/usertools", {
    "label": "Body Demo", "url": "https://93.184.216.34/bb",
    "timeout_s": 30, "files_min": 0, "files_max": 0,
    "body_format": "text",
    "body_placeholder": {"en": "Paste text"},
    "cost": 0},
    {"Content-Type": "application/json", "Cookie": ck})
key = (r or {}).get("tool_key") or ""
check("A1 body tool created", st == 200 and r.get("ok") and key,
      f"{st} {str(r)[:90]}")
with db() as con:
    row = con.execute(
        "select body_format, body_placeholder_json, accept_json,"
        " format_family from platform_tools where tool_key=%s",
        (key,)).fetchone()
check("A2 stored row: body columns set, file columns NULL",
      row["body_format"] == "text"
      and json.loads(row["body_placeholder_json"]) == {"en": "Paste text"}
      and row["accept_json"] is None and row["format_family"] is None,
      str(dict(row)) if row else "no row")

# owner projection carries the body fields
st, r = _req("GET", "/api/usertools", None, {"Cookie": ck})
mine = next((t for t in (r.get("tools") or []) if t["tool_key"] == key), {})
check("A3 owner projection exposes body_format/placeholder",
      mine.get("body_format") == "text"
      and mine.get("body_placeholder") == {"en": "Paste text"}, str(mine)[:110])

# mutual-exclusion rejects
for payload, code, why in (
    ({"label": "X", "url": "https://93.184.216.34/x", "timeout_s": 30,
      "files_min": 0, "files_max": 0, "body_format": "text",
      "accept": [".png"]}, "bad_accept", "body tool with accept"),
    ({"label": "X", "url": "https://93.184.216.34/x", "timeout_s": 30,
      "files_min": 1, "files_max": 1, "format_families": "image",
      "accept": [".png"], "body_format": "text"},
     "bad_body_format", "file tool with body_format"),
    ({"label": "X", "url": "https://93.184.216.34/x", "timeout_s": 30,
      "files_min": 0, "files_max": 0}, "bad_body_format",
     "files 0..0 without body_format"),
):
    st, r = _req("POST", "/api/usertools", payload,
                 {"Content-Type": "application/json", "Cookie": ck})
    check(f"A4 {why} → 422 {code}",
          st == 422 and r.get("error") == code, f"{st} {str(r)[:70]}")

# PATCH: text → json (placeholder preserved); json → file (mutex columns
# cleared server-side, same transaction); file → body again
st, r = _req("PATCH", f"/api/usertools/{key}", {"body_format": "json"},
             {"Content-Type": "application/json", "Cookie": ck})
with db() as con:
    row = con.execute(
        "select body_format, body_placeholder_json from platform_tools"
        " where tool_key=%s", (key,)).fetchone()
check("A5 PATCH body_format text→json, placeholder preserved",
      st == 200 and row["body_format"] == "json"
      and json.loads(row["body_placeholder_json"]) == {"en": "Paste text"},
      f"{st} {str(dict(row)) if row else ''}")
st, r = _req("PATCH", f"/api/usertools/{key}",
             {"files_min": 1, "files_max": 1, "format_families": "image",
              "accept": [".png"]},
             {"Content-Type": "application/json", "Cookie": ck})
with db() as con:
    row = con.execute(
        "select body_format, body_placeholder_json, accept_json,"
        " format_family from platform_tools where tool_key=%s",
        (key,)).fetchone()
check("A6 PATCH switch body→file clears body columns",
      st == 200 and row["body_format"] is None
      and row["body_placeholder_json"] is None
      and row["accept_json"] is not None and row["format_family"] == "image",
      f"{st} {str(dict(row)) if row else ''}")
st, r = _req("PATCH", f"/api/usertools/{key}",
             {"files_min": 0, "files_max": 0, "body_format": "text"},
             {"Content-Type": "application/json", "Cookie": ck})
with db() as con:
    row = con.execute(
        "select body_format, accept_json, format_family"
        " from platform_tools where tool_key=%s", (key,)).fetchone()
check("A7 PATCH switch file→body clears file columns",
      st == 200 and row["body_format"] == "text"
      and row["accept_json"] is None and row["format_family"] is None,
      f"{st} {str(dict(row)) if row else ''}")

# F4: update_tool PATCH 终态防御 — 对正文工具只带 body_format:None 的
# PATCH 会留下 files_max=0 + body_format NULL 的互斥违例终态; 单点在
# 写库前拒绝, 库内无变化 (route 层的输入方式切换组装不触发此路径 —
# 这是 supply 单点的防御层)。
from governance.tool_supply import update_tool, DeclarationError
_f4 = False
try:
    update_tool(key, {"body_format": None})
except DeclarationError as exc:
    _f4 = str(exc) == "bad_body_format"
except Exception as exc:  # noqa: BLE001
    _f4 = f"unexpected {type(exc).__name__}: {exc}"
with db() as con:
    row = con.execute(
        "select files_max, body_format from platform_tools where tool_key=%s",
        (key,)).fetchone()
check("F4 inconsistent final state (files 0..0, body_format NULL) → rejected, row unchanged",
      _f4 is True and row["files_max"] == 0 and row["body_format"] == "text",
      f"{_f4} {dict(row) if row else ''}")

# ═══ B. invocation-level contract through the app ═══
def _exec(source, ref):
    return _req("POST", "/api/tools/execute", {
        "tool_id": "share",
        "source": source,
        "params": {},
        "delivery": {"expiry_days": 1, "allowed_downloads": 50},
        "client_ref": ref},
        {"Content-Type": "application/json", "Cookie": ck})

st, r = _exec({"body": "hello"}, f"bt-filetool-body-{_u}")
check("B1 body source to a file tool → 422 err_input_mode",
      st == 422 and r.get("error") == "err_input_mode", f"{st} {str(r)[:70]}")

def _exec_body(tool, source, ref):
    return _req("POST", "/api/tools/execute", {
        "tool_id": tool,
        "source": source,
        "params": {},
        "delivery": {"expiry_days": 1, "allowed_downloads": 50},
        "client_ref": ref},
        {"Content-Type": "application/json", "Cookie": ck})

st, r = _exec_body(key, {"files": []}, f"bt-emptyfiles-{_u}")
check("B2 files[] source to a body tool → 422 err_input_mode",
      st == 422 and r.get("error") == "err_input_mode", f"{st} {str(r)[:70]}")

def _exec_body_params(params, ref):
    return _req("POST", "/api/tools/execute", {
        "tool_id": key,
        "source": {"body": "x"},
        "params": params,
        "delivery": {"expiry_days": 1, "allowed_downloads": 50},
        "client_ref": ref},
        {"Content-Type": "application/json", "Cookie": ck})

st, r = _exec_body_params({"opt": {"a": 1}}, f"bt-pdict-{_u}")
check("B2b body tool param dict value → 422 param_invalid (field)",
      st == 422 and r.get("error") == "param_invalid"
      and r.get("field") == "opt", f"{st} {str(r)[:80]}")
st, r = _exec_body_params({"opt": None}, f"bt-pnull-{_u}")
check("B2c body tool param null value → 422 param_invalid (field)",
      st == 422 and r.get("error") == "param_invalid"
      and r.get("field") == "opt", f"{st} {str(r)[:80]}")
st, r = _exec_body_params({"opt": [1, 2]}, f"bt-plist-{_u}")
check("B2d body tool param list value → 422 param_invalid (field)",
      st == 422 and r.get("error") == "param_invalid", f"{st} {str(r)[:80]}")
st, r = _exec_body(key, {"body": ""}, f"bt-emptybody-{_u}")
check("B3 empty text body → 422 err_body_required",
      st == 422 and r.get("error") == "err_body_required", f"{st} {str(r)[:70]}")
st, r = _exec_body(key, {"body": "x" * 100001}, f"bt-longbody-{_u}")
check("B4 text body over 100k → 422 err_body_too_long",
      st == 422 and r.get("error") == "err_body_too_long", f"{st} {str(r)[:70]}")
with db() as con:
    n_calls = con.execute(
        "select count(*) as n from tool_calls where user_id=%s and"
        " client_ref like %s", (uid, f"bt-%{_u}")).fetchone()["n"]
check("B5 all denials above left zero tool_calls",
      n_calls == 0, n_calls)

# ═══ C. parser + fingerprint (direct, no server state) ═══
from execution.invocation import (parse_tool_invocation, InvocationError,
                                  invocation_fingerprint, ToolInvocation,
                                  BODY_ABSENT)

good_body = {"tool_id": "x", "source": {"body": None},
             "params": {}, "delivery": {"expiry_days": 1,
                                        "allowed_downloads": 50},
             "client_ref": "r1"}
inv = parse_tool_invocation(good_body)
check("C1 body:null parses (key-presence semantics)",
      inv.has_body() and inv.body is None and inv.files == [])
try:
    parse_tool_invocation({**good_body, "source": {}})
    check("C2 empty source rejected", False, "accepted")
except InvocationError:
    check("C2 empty source rejected", True)
try:
    parse_tool_invocation({**good_body,
                           "source": {"files": [], "body": "x"}})
    check("C3 files+body both rejected", False, "accepted")
except InvocationError:
    check("C3 files+body both rejected", True)


def _mkbody(value, fmt):
    i = ToolInvocation("x", [], {}, {"expiry_days": 1,
                                     "allowed_downloads": 50}, "r")
    i.body = value
    i.body_format = fmt
    return i

h1 = invocation_fingerprint(_mkbody({"b": 2, "a": 1}, "json"))
h2 = invocation_fingerprint(_mkbody({"a": 1, "b": 2}, "json"))
check("C4 json body canonical equivalence → same fingerprint",
      h1 == h2 and len(h1) == 64)
check("C5 json body value change → different fingerprint",
      invocation_fingerprint(_mkbody({"a": 1}, "json"))
      != invocation_fingerprint(_mkbody({"a": 2}, "json")))
check("C6 text body fingerprint deterministic",
      invocation_fingerprint(_mkbody("abc", "text"))
      == invocation_fingerprint(_mkbody("abc", "text")))
check("C7 body vs files fingerprint differ",
      invocation_fingerprint(_mkbody("abc", "text"))
      != invocation_fingerprint(ToolInvocation(
          "x", [], {}, {"expiry_days": 1, "allowed_downloads": 50}, "r")))

# F5: canonical_body_payload 严格化 — body_format 未注入即抛错, 不按
# 值形态推断、无 repr 兜底 (调用方契约缺口必须显性失败)。
from execution.invocation import canonical_body_payload
try:
    canonical_body_payload(_mkbody("abc", None))
    check("F5 canonical_body_payload without body_format raises", False,
          "accepted")
except RuntimeError:
    check("F5 canonical_body_payload without body_format raises", True)

# ═══ D. real worker subprocess: temp body tool via load_all ═══
_tag = _os.urandom(4).hex()
_mod_name = f"tools_tmpbody_{_tag}"
_ttext = f"tmpbodyt_{_tag}"
_tjson = f"tmpbodyj_{_tag}"
_tmp = _os.path.join(_REPO, "core", _mod_name + ".py")
with open(_tmp, "w") as f:
    f.write(f'''
import json as _json
from tools import register


def _echo(tool_input, params):
    return {{"items": [{{"kind": "text", "text": _json.dumps(
        {{"body": tool_input.get("body"), "params": params}},
        ensure_ascii=False)}}]}}


register(tool_id="{_ttext}", label={{"en": "TmpBodyT"}}, handler=_echo,
         files_min=0, files_max=0, outputs=("text",),
         body_format="text", body_placeholder={{"en": "Paste text"}})
register(tool_id="{_tjson}", label={{"en": "TmpBodyJ"}}, handler=_echo,
         files_min=0, files_max=0, outputs=("text",),
         body_format="json")
''')
try:
    _core_dir = _os.path.join(_REPO, "core")
    runner = (
        "import sys, json\n"
        f"sys.path[:0] = [{_REPO!r}, {_core_dir!r}]\n"
        "from execution.worker import process_tool\n"
        "req = json.load(sys.stdin)\n"
        "out = process_tool(req['tool'], [], req['params'], req['body'])\n"
        "json.dump(out, sys.stdout)\n")
    def _worker_call(tool, body, params=None):
        proc = subprocess.run(
            [_sys.executable, "-c", runner],
            input=json.dumps({"tool": tool, "body": body,
                              "params": params or {}}),
            capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            return None, proc.stderr[-400:]
        try:
            return json.loads(proc.stdout), None
        except Exception as exc:
            return None, f"bad stdout: {proc.stdout[:200]} {exc}"

    out, err = _worker_call(_ttext, "hello body", {"k": "v"})
    parsed = json.loads(out["items"][0]["text"]) if out else {}
    check("D1 text body through real worker subprocess (load_all re-run)",
          bool(out) and parsed == {"body": "hello body", "params": {"k": "v"}},
          err or str(out)[:110])
    out, err = _worker_call(_tjson, {"b": 2, "a": 1})
    parsed = json.loads(out["items"][0]["text"]) if out else {}
    check("D2 json object body survives the process boundary",
          bool(out) and parsed["body"] == {"a": 1, "b": 2}, err or str(out)[:110])
    out, err = _worker_call(_tjson, None)
    parsed = json.loads(out["items"][0]["text"]) if out else {}
    check("D3 json null body round-trips as None",
          bool(out) and parsed["body"] is None, err or str(out)[:110])
finally:
    try:
        _os.remove(_tmp)
    except OSError:
        pass

# ═══ cleanup ═══
with db() as con:
    con.execute("delete from platform_tools where owner_id=%s", (uid,))
    purge_users(con, [uid])

print()
if FAIL:
    print(f"FAIL: {FAIL}")
    _sys.exit(1)
print(f"OK — body tool contract ({COUNT} checks)")
