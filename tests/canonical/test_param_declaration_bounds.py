"""Number parameter declarations must stay finite and self-consistent.

float() accepts inf/nan, and the declaration validator historically
didn't reject them — a public tool with a non-finite bound poisoned
JSON serialization and took the anonymous MCP tools/list down with a
500. The validator now rejects non-finite min/max/default and defaults
outside their own bounds; the inputSchema projection additionally drops
non-finite values so legacy rows can never break the catalog.
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

import http.client as _hc
import json
import time
import uuid
ts = int(time.time())
_u = "pb" + _os.urandom(3).hex()
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
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    conn.request("POST", "/auth/register", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    conn.close()
    b, ct = _form({"login": name, "password": "Test12345"})
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close()
    return ck


def _mktool(ck, name_suffix, params):
    return _req("POST", "/api/usertools", {
        "tool_id": "pb" + uuid.uuid4().hex[:8],
        "label": f"PB {name_suffix}", "url": "https://93.184.216.34/pb",
        "timeout_s": 30, "files_min": 0, "files_max": 1,
        "format_families": "image",
        "accept": [".png"], "cost": 0,
        "params": params}, {"Content-Type": "application/json", "Cookie": ck})


ck = _register_login(_u)
with db() as con:
    uid = con.execute("select id from users where username=%s", (_u,)).fetchone()["id"]

# ── 1. declaration gate: non-finite bounds are rejected ──
for label, params in [
    ("inf min", [{"key": "a", "type": "number", "min": "inf", "max": 10}]),
    ("nan default", [{"key": "a", "type": "number", "min": 0, "max": 10,
                      "default": "nan"}]),
    ("inf max", [{"key": "a", "type": "number", "max": float("inf")}]),
]:
    st, r = _mktool(ck, label.replace(" ", ""), params)
    check(f"1 '{label}' declaration → 422 bad_param_default",
          st == 422 and r.get("error") == "bad_param_default", f"{st} {str(r)[:70]}")

# ── 2. default must satisfy its own bounds ──
st, r = _mktool(ck, "range", [{"key": "a", "type": "number", "min": 1,
                               "max": 10, "default": 0}])
check("2 out-of-range default → 422 bad_param_default",
      st == 422 and r.get("error") == "bad_param_default", f"{st} {str(r)[:70]}")

# ── 3. control: a legal bounded default still passes ──
st, r = _mktool(ck, "ok", [{"key": "a", "type": "number", "min": 1,
                            "max": 10, "default": 5}])
check("3 legal bounded declaration accepted", st == 200 and r.get("ok"),
      f"{st} {str(r)[:70]}")

# ── 4. projection defense: a legacy non-finite row must not break
#      the anonymous MCP tools/list ──
legacy_key = "pb" + uuid.uuid4().hex[:8]
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id,"
        " label_json, accept_json, files_min, files_max, endpoint_json,"
        " params_json, featured, created_at, updated_at)"
        " values (%s, %s, %s, %s, %s, 0, 1, %s, %s, TRUE, %s, %s)",
        (legacy_key, uid, legacy_key, '{"en": "PB Legacy"}', '[".png"]',
         json.dumps({"url": "https://93.184.216.34/legacy", "timeout_s": 30}),
         json.dumps([{"key": "x", "type": "number",
                      "min": float("inf"), "max": float("nan"),
                      "default": float("inf")}]),
         ts, ts))
    con.execute(
        "insert into pricing (tool_id, cost, enabled, updated_at)"
        " values (%s, 0, TRUE, %s)", (legacy_key, ts))
st, r = _req("POST", "/mcp",
             {"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
             {"Content-Type": "application/json"})
tools = ((r.get("result") or {}).get("tools") or []) if isinstance(r, dict) else []
legacy = next((t for t in tools
               if str(t.get("name", "")).endswith(legacy_key)), None)
schema = ((legacy or {}).get("inputSchema") or {}).get("properties", {}).get("x", {})
check("4 anonymous tools/list survives a legacy non-finite declaration",
      st == 200 and legacy is not None,
      f"{st} names={[t.get('name') for t in tools][:8]}")
check("4b non-finite bound/default dropped from the projected schema",
      legacy is not None
      and "minimum" not in schema and "maximum" not in schema
      and "default" not in schema,
      json.dumps(schema)[:100])

with db() as con:
    con.execute("delete from platform_tools where owner_id=%s", (uid,))
    purge_users(con, [uid])

print()
if FAIL:
    print(f"FAIL: {FAIL}")
    _sys.exit(1)
print(f"OK — param declaration bounds ({COUNT} checks)")
