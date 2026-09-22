#!/usr/bin/env python3
"""Hosted-endpoint execution capability — authorization seam canonical.

/tool-endpoints/* is authorized ONLY by the short-lived HMAC
hosted-execution capability minted by canonical trusted dispatch for a
trusted same-origin hosted endpoint URL, bound to the exact path + the
outbound payload digest. Proves, over real HTTP + real DB:

  HX1  ordinary API Bearer alone cannot authorize the endpoint
  HX2  ordinary Bearer + otherwise-valid signed source URL still denied
  HX3  denied attempts never redeem the source capability (one-time
       fetch token stays unredeemed when hosted auth fails first)
  HX4  canonical trusted hosted dispatch → endpoint accepts, tool runs
  HX5  untrusted (user-owned) tool pointing at the same hosted URL gets
       NO capability (mint refuses non-trusted dispatch payloads)
  HX6  capability is payload-bound: minted cap + different job_inputs →
       rejected (cannot splice onto another set of source URLs)
  HX7  /mcp Bearer behavior unchanged (tools/list still 200)
  HX8  external (non-hosted) remote HTTP execution unchanged (mint
       returns None for foreign URLs; drive_http path untouched)
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
_os.environ.setdefault("FLASHSHARE_GOKAPI_URL", "http://127.0.0.1:53898")
_os.environ.setdefault("FLASHSHARE_GOKAPI_KEY", "ci-gokapi-key")

FAIL = []
COUNT = 0


def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓ " if ok else "✗ ") + name + (f" — {detail}" if (detail and not ok) else ""))
    if not ok:
        FAIL.append(name)


import base64 as _b64
import http.client as _hc
import io as _io
import json as _json
import uuid as _uuid

HOST = ("127.0.0.1", int(_TEST_PORT))
BASE = f"http://127.0.0.1:{_TEST_PORT}"


def _mp(fields, files=None):
    out = b""
    for k, v in fields.items():
        out += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for name, ct, data in (files or []):
        out += (f'--B\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                f'Content-Type: {ct}\r\n\r\n').encode() + data + b"\r\n"
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _req(method, path, body=None, headers=None, raw_body=None, ctype="application/json"):
    conn = _hc.HTTPConnection(*HOST, timeout=60)
    h = {"Content-Type": ctype}
    if headers:
        h.update(headers)
    conn.request(method, path,
                 raw_body if raw_body is not None else
                 (_json.dumps(body) if body is not None else None), h)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        data = _json.loads(raw)
    except Exception:
        data = {"_raw": raw[:120].decode("utf-8", "replace")}
    conn.close()
    return resp.status, data


# ── user + MCP token (ordinary API Bearer) ──
from PIL import Image  # noqa: E402

_u = "hx" + _uuid.uuid4().hex[:6]
_b, _ct = _mp({"username": _u, "password": "Test12345", "password_confirm": "Test12345",
               "email": f"{_u}@t.co"})
_c = _hc.HTTPConnection(*HOST, timeout=30)
_c.request("POST", "/auth/register", _b, {"Content-Type": _ct})
_c.getresponse().read(); _c.close()
_c = _hc.HTTPConnection(*HOST, timeout=30)
_b, _ct = _mp({"login": _u, "password": "Test12345"})
_c.request("POST", "/auth/login", _b, {"Content-Type": _ct})
_ck = (_c.getresponse().getheader("Set-Cookie") or "").split(";")[0]
_c.close()
_st, _d = _req("POST", "/api/tokens", {"name": "hx"}, {"Cookie": _ck})
TOK = (_d.get("token") or {}).get("token")
check("setup ordinary API token minted", bool(TOK))

# ── a real signed source URL (valid fetch token, not yet redeemed) ──
_buf = _io.BytesIO()
Image.new("RGB", (60, 60), "red").save(_buf, "PNG")
_b, _ct = _mp({"client_ref": "hx-" + _uuid.uuid4().hex[:8], "tool": "privacy"},
              [("a.png", "image/png", _buf.getvalue())])
_st, _d = _req("POST", "/api/uploads", None, {"Cookie": _ck}, raw_body=_b, ctype=_ct)
_f0 = (_d.get("files") or [{}])[0] if _d.get("files") else (_d.get("file") or {})
GID = _f0.get("publicId")
SRC_URL = f"{BASE}/api/source/{GID}?u=&t="  # real gid; token minted below


def _fetch_url():
    from security import sign_fetch_token
    with __import__("database").db() as con:
        row = con.execute("select id from users where username=%s", (_u,)).fetchone()
    return f"{BASE}/api/source/{GID}?u={row['id']}&t={sign_fetch_token(int(row['id']), GID)}"


JOB = [{"filename": "a.png", "content_type": "image/png", "size": 96, "url": _fetch_url()}]

# ── HX1: ordinary Bearer alone → 401 ──
_st, _d = _req("POST", "/tool-endpoints/rounded-corners",
               {"job_inputs": JOB, "params": {}},
               {"Authorization": f"Bearer {TOK}"})
check("HX1 ordinary Bearer alone → 401 auth_required", _st == 401 and _d.get("error") == "auth_required",
      f"{_st} {_d}")

# ── HX2: Bearer + valid signed source URL still denied ──
_st, _d = _req("POST", "/tool-endpoints/rounded-corners",
               {"job_inputs": JOB, "params": {"radius": "medium", "background": "transparent"}},
               {"Authorization": f"Bearer {TOK}"})
check("HX2 Bearer + valid signed source URL → 401", _st == 401 and _d.get("error") == "auth_required",
      f"{_st} {_d}")

# ── HX3: failed attempt did not redeem the source fetch token ──
# (fresh token; attempt with it denied → the same token still verifies)
from security import sign_fetch_token, verify_fetch_token  # noqa: E402
with __import__("database").db() as con:
    _uid_row = con.execute("select id from users where username=%s", (_u,)).fetchone()
UID = int(_uid_row["id"])
_tok_probe = sign_fetch_token(UID, GID)
_body2 = {"job_inputs": [{"filename": "a.png", "content_type": "image/png",
                          "size": 96, "url": f"{BASE}/api/source/{GID}?u={UID}&t={_tok_probe}"}],
          "params": {}}
_st, _d = _req("POST", "/tool-endpoints/rounded-corners", _body2,
               {"Authorization": f"Bearer {TOK}"})
check("HX3a denied again with fresh token", _st == 401)
check("HX3b source capability 未被消费 (fetch token 仍可验证)",
      verify_fetch_token(_tok_probe, UID, GID))

# ── capability primitives + mint boundary (in-process) ──
from security import (sign_hosted_exec_token, verify_hosted_exec_token,  # noqa: E402
                      hosted_payload_digest)
# canonical outbound payload: exactly what dispatch would send for this call
_PARAMS = {"radius": "medium", "background": "transparent"}
_PAYLOAD = {"job_inputs": JOB, "params": _PARAMS}
_cap = sign_hosted_exec_token("/tool-endpoints/rounded-corners", _PAYLOAD)
check("HX4a mint/verify roundtrip", verify_hosted_exec_token(_cap, "/tool-endpoints/rounded-corners",
                                                             _PAYLOAD))
check("HX4b wrong path rejected",
      not verify_hosted_exec_token(_cap, "/tool-endpoints/other", _PAYLOAD))

# HX4c: full canonical path — trusted same-origin dispatch payload accepted live
_st, _d = _req("POST", "/tool-endpoints/rounded-corners", _PAYLOAD,
               {"X-Hosted-Execution": _cap})
check("HX4c trusted dispatch capability → endpoint accepts & runs",
      _st == 200 and isinstance(_d.get("data_b64"), str) and len(_d["data_b64"]) > 0,
      f"{_st} {str(_d)[:120]}")

# ── HX5: trust dimension of the mint gate (explicit trusted input) ──
import execution.orchestrator as _orch  # noqa: E402
_orig_base = _orch.BASE_URL
_orch.BASE_URL = BASE  # same process config as the app under test
_mint_hosted_capability = _orch._mint_hosted_capability
_srcs = [{"filename": "a.png", "content_type": "image/png", "size": 96,
          "fetch_url": f"{BASE}/api/source/{GID}?u={UID}&t=x"}]
_HOSTED_URL = f"{BASE}/tool-endpoints/rounded-corners"
check("HX5a trusted=False + exact same-origin hosted URL → None (fail closed)",
      _mint_hosted_capability(_HOSTED_URL, _srcs, {}, trusted=False) is None)
check("HX5b trusted=True + exact same-origin hosted URL → capability minted",
      _mint_hosted_capability(_HOSTED_URL, _srcs, {}, trusted=True) is not None)
check("HX5c trusted=True + foreign origin → None",
      _mint_hosted_capability("https://dev.example/api/tool", _srcs, {}, trusted=True) is None)
check("HX5d trusted=True + same-origin non-/tool-endpoints/* path → None",
      _mint_hosted_capability(f"{BASE}/api/other", _srcs, {}, trusted=True) is None)

# HX5e: execute_remote's canonical trusted fact is what reaches the mint —
# untrusted rows mint nothing even at the canonical call boundary.
import inspect as _inspect  # noqa: E402
_exr_src = _inspect.getsource(_orch.execute_remote)
check("HX5e execute_remote 传入其已计算的 canonical trusted fact",
      'trusted = (tool.get("owner_id") is None) or bool(tool.get("trusted"))' in _exr_src
      and "_mint_hosted_capability(decl.get(\"url\") or \"\", sources, params, trusted)" in _exr_src)

# ── HX6: payload binding — cap + different job_inputs → rejected ──
JOB2 = [{"filename": "b.png", "content_type": "image/png", "size": 96,
         "url": f"{BASE}/api/source/{GID}?u={UID}&t=other"}]
_st, _d = _req("POST", "/tool-endpoints/rounded-corners",
               {"job_inputs": JOB2, "params": {}}, {"X-Hosted-Execution": _cap})
check("HX6 capability 不能拼接另一组 source URLs", _st == 401, f"{_st}")

# ── HX7: /mcp Bearer behavior unchanged ──
_st, _d = _req("POST", "/mcp",
               {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
               {"Authorization": f"Bearer {TOK}"})
_tools = (_d.get("result") or {}).get("tools")
check("HX7 /mcp Bearer tools/list unchanged (200 + tools)",
      _st == 200 and isinstance(_tools, list) and len(_tools) > 0, f"{_st}")

# ── HX8: external remote HTTP execution unchanged (mint None for foreign) ──
import remote  # noqa: E402
_rec = {"called": None}


class _FakeResp:
    def __init__(self, body):
        self.status = 200
        self.headers = {"Content-Type": "application/json"}
        self._body = body

    def read(self, _n=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(req, timeout=None):
    _rec["called"] = dict(req.headers)
    return _FakeResp(b'{"data_b64": "aGk=", "filename": "out.txt"}')


_orig_urlopen = remote.urllib.request.urlopen
remote.urllib.request.urlopen = _fake_urlopen
try:
    out = remote.drive_http({"url": "https://dev.example/api/tool"}, [], {}, {}, trusted=True)
    check("HX8 external remote HTTP execution unchanged",
          out["bytes"] == b"hi" and "X-Hosted-Execution" not in (_rec["called"] or {}),
          f"{out} {_rec['called']}")
finally:
    remote.urllib.request.urlopen = _orig_urlopen

print()
print("FAIL: " + (", ".join(FAIL) if FAIL else f"无 — 全绿 ({COUNT} checks)"))
_sys.exit(1 if FAIL else 0)
