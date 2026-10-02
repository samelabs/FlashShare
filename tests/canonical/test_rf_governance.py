#!/usr/bin/env python3
"""R/F governance canonical.

  S*  Share cards: an image file page and a result page with images use
      the image's inline URL as og:image; the file page card names the
      file and its facts.
  C*  Continue processing: /api/results/{id}/files is owner-only; MCP
      result file items carry public_id, and a later call can take that
      public_id as a reference (no re-upload).
"""
import base64
import http.client as _hc
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
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:160]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)


_HOST = ("127.0.0.1", int(_TEST_PORT))
_u = "rf" + _uuid.uuid4().hex[:6]
ts = int(_time.time())

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")
PDF = b"%PDF-1.4\n%%EOF\n"


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=120)
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else _json.dumps(body).encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    setc = r.getheader("Set-Cookie") or ""
    conn.close()
    try:
        return r.status, hdrs, (_json.loads(data) if data else None), setc
    except Exception:
        return r.status, hdrs, data.decode(errors="replace"), setc


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _mcp(body, token):
    st, _h, d, _sc = _req("POST", "/mcp", body, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})
    return st, d


def _call(name, arguments, token, mid=1):
    return _mcp({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token)


def _b64(b):
    return base64.b64encode(b).decode()


def _tool_text(d):
    """content[0].text parsed as JSON (the unified isError shape)."""
    try:
        return _json.loads(
            (((d or {}).get("result") or {}).get("content")
             or [{}])[0].get("text") or "{}")
    except Exception:
        return {}



def _register(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": name, "password": "Test12345"})
    _st, _h, _d, sc = _req("POST", "/auth/login", b, {"Content-Type": ct})
    ck = sc.split(";")[0]
    _st, _h, d_k, _sc = _req("POST", "/api/keys", {"name": "rf"},
                             {"Content-Type": "application/json", "Cookie": ck})
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck, d_k["key"]["key"]


from database import db  # noqa: E402
import re as _re  # noqa: E402


def _png():
    from PIL import Image
    import io
    b = io.BytesIO()
    Image.new("RGB", (64, 48), (20, 120, 200)).save(b, "PNG")
    return b.getvalue()


uids = []
try:
    a_uid, a_ck, A_TOK = _register(_u + "a")
    b_uid, b_ck, B_TOK = _register(_u + "b")
    uids = [a_uid, b_uid]
    st, d = _call("share", {"client_ref": f"rf1-{_u}", "files": [
        {"filename": "pic.png", "b64": _b64(_png())},
        {"filename": "doc.pdf", "b64": _b64(PDF)}]}, A_TOK)
    sc = ((d or {}).get("result") or {}).get("structuredContent") or {}
    files = [i for i in sc.get("items", []) if i.get("kind") == "file"]
    check("C1 MCP result file items carry public_id",
          len(files) == 2 and all(f.get("public_id") for f in files), sc)
    rid = (sc.get("result_url") or "").rsplit("/", 1)[-1]
    img_pid = next((f["public_id"] for f in files
                    if f["filename"].endswith(".png")), "")

    # ═══ S: share cards ═══
    _st, _h, page, _sc = _req("GET", f"/f/{img_pid}", None, {"Accept": "text/html"})
    og = _re.search(r'property="og:image" content="([^"]+)"', page or "")
    check("S1 /f image page og:image is the inline view",
          og and og.group(1).endswith(f"/f/{img_pid}/inline"), og and og.group(1))
    check("S2 /f card title is the file name",
          'property="og:title" content="pic.png"' in (page or ""))
    _st, _h, page, _sc = _req("GET", f"/r/{rid}", None, {"Accept": "text/html"})
    og = _re.search(r'property="og:image" content="([^"]+)"', page or "")
    check("S3 /r with an image uses it as og:image",
          og and og.group(1).endswith(f"/f/{img_pid}/inline"), og and og.group(1))

    # ═══ C: continue processing ═══
    st, _h, d, _sc = _req("GET", f"/api/results/{rid}/files", None, {"Cookie": a_ck})
    check("C2 owner reads the result file list",
          st == 200 and len((d or {}).get("files") or []) == 2, f"{st} {d}")
    st, _h, d, _sc = _req("GET", f"/api/results/{rid}/files", None, {"Cookie": b_ck})
    check("C3 another user gets 404", st == 404, st)
    _st, _h, page, _sc = _req("GET", f"/r/{rid}", None,
                              {"Accept": "text/html", "Cookie": a_ck})
    check("C4 owner sees the continue button",
          f'href="/?from={rid}"' in (page or ""))
    _st, _h, page, _sc = _req("GET", f"/r/{rid}", None, {"Accept": "text/html"})
    check("C5 visitors do not", f'href="/?from={rid}"' not in (page or ""))
    st, d = _call("compress", {"client_ref": f"rf2-{_u}",
                               "files": [{"public_id": img_pid}]}, A_TOK)
    res = (d or {}).get("result") or {}
    check("C6 a returned public_id chains into the next call",
          res.get("isError") is False, str(d)[:200])
finally:
    if uids:
        with db() as con:
            purge_users(con, uids)

print(f"\n{COUNT - len(FAIL)}/{COUNT} checks passed")
_sys.exit(1 if FAIL else 0)
