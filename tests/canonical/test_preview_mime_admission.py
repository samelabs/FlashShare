"""Preview MIME admission: parameterized MIME must classify identically
to its bare form at every preview consumer.

Upload stores the client-declared content_type verbatim (it may carry
'; param' suffixes). Admission and projection comparisons must use the
normalized base type — the historical full-string SVG exclusion let
`image/svg+xml; charset=utf-8` through to the same-origin inline
channel while the upload side still treated it as SVG.
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
import time
import uuid
ts = int(time.time())
_u = "pm" + _os.urandom(3).hex()
_HOST = ("127.0.0.1", int(_TEST_PORT))

from database import db

GK_URL = _os.environ["FLASHSHARE_GOKAPI_URL"].rstrip("/")
GK_KEY = _os.environ["FLASHSHARE_GOKAPI_KEY"]


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


def _gk_add(filename, content_type, data):
    """Store a real file in the (fake) Gokapi; returns its file id."""
    import urllib.request as _ur
    b = "----gk"
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="file";'
             f' filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode()
             + data + b"\r\n",
             f'--{b}\r\nContent-Disposition: form-data; name="expiryDays"'
             f'\r\n\r\n1\r\n'.encode(),
             f'--{b}\r\nContent-Disposition: form-data;'
             f' name="allowedDownloads"\r\n\r\n50\r\n'.encode(),
             f"--{b}--\r\n".encode()]
    body = b"".join(parts)
    req = _ur.Request(GK_URL + "/api/files/add", data=body,
                      headers={"apikey": GK_KEY,
                               "Content-Type": f"multipart/form-data; boundary={b}"})
    with _ur.urlopen(req, timeout=10) as r:
        info = json.loads(r.read())["FileInfo"]
    return info["Id"]


def _insert_upload(uid, gokapi_id, public_id, file_name, content_type, size):
    with db() as con:
        con.execute(
            "insert into uploads (user_id, ip, gokapi_id, public_id,"
            " file_name, content_type, size_bytes, expiry_days,"
            " allowed_downloads, expire_at, url_download, created_at)"
            " values (%s, '127.0.0.1', %s, %s, %s, %s, %s, 1, 50, %s, %s, %s)",
            (uid, gokapi_id, public_id, file_name, content_type, size,
             ts + 3600, f"{GK_URL}/downloadFile/{gokapi_id}", ts))


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
# Harmless marker content (no script/external refs) — the fixture is only
# about the parameterized MIME; admission must still classify it as SVG.
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><text>x</text></svg>'

ck = _register_login(_u)
with db() as con:
    uid = con.execute("select id from users where username=%s", (_u,)).fetchone()["id"]

# ── 1. parameterized-SVG row: stored verbatim (upload side keeps params) ──
pid_svg = "pm" + uuid.uuid4().hex[:10]
_insert_upload(uid, "gk-unused-svg", pid_svg, "mark.svg",
               "image/svg+xml; charset=utf-8", len(SVG))
with db() as con:
    stored = con.execute("select content_type from uploads where public_id=%s",
                         (pid_svg,)).fetchone()["content_type"]
check("1 stored MIME carries the parameter suffix (fixture is real)",
      stored.lower().startswith("image/svg+xml;"), str(stored))

# ── 2. the same-origin inline channel must refuse it ──
st, r = _req("GET", f"/f/{pid_svg}/inline", cookies=ck)
check("2 inline preview refuses parameterized SVG (not_embeddable)",
      st == 400 and r.get("error") == "not_embeddable", f"{st} {str(r)[:80]}")

# ── 3. share page projection classifies parameterized SVG as non-image ──
st, raw = _req("GET", f"/f/{pid_svg}", cookies=ck)
body_html = raw if isinstance(raw, str) else raw.decode(errors="replace")
check("3 share page renders the non-image path (no inline embed)",
      st == 200 and "mark.svg" in body_html, f"{st} {str(raw)[:60]}")

# ── 4. control: bare PNG (real Gokapi object) is still admitted inline ──
gk_png = _gk_add("a.png", "image/png", PNG)
pid_png = "pm" + uuid.uuid4().hex[:10]
_insert_upload(uid, gk_png, pid_png, "a.png", "image/png", len(PNG))
st, r = _req("GET", f"/f/{pid_png}/inline", cookies=ck)
check("4 control: bare PNG inline still admitted", st == 200, f"{st} {str(r)[:60]}")

# ── 5. unit level: the base-type helper is the single normalizer ──
_os.environ.setdefault("FLASHSHARE_BASE_URL", f"http://127.0.0.1:{_TEST_PORT}")
from utils import mime_base  # noqa: E402
for raw_in, expect in [
    ("image/svg+xml; charset=utf-8", "image/svg+xml"),
    ("IMAGE/SVG+XML", "image/svg+xml"),
    ("  application/pdf ; name=x ", "application/pdf"),
    ("", ""),
    (None, ""),
]:
    got = mime_base(raw_in)
    check(f"5 mime_base({raw_in!r}) == {expect!r}", got == expect, got)

with db() as con:
    purge_users(con, [uid])

print()
if FAIL:
    print(f"FAIL: {FAIL}")
    _sys.exit(1)
print(f"OK — preview MIME admission ({COUNT} checks)")
