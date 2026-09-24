#!/usr/bin/env python3
"""Sharing content authority canonical — stored MIME, preview gate, headers.

Locks: the stored content_type is derived server-side from the sniffed
extension (declared MIME never becomes a fact); inline preview is an exact
allowlist shared by the detail page and the endpoint; every file response
carries an RFC 5987 Content-Disposition (non-ASCII names never 500);
filename truncation keeps the extension; endpoint-provided result names
are sanitized; the market search box escapes its reflected query.
Real HTTP surfaces + isolated DB + fake Gokapi.
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
_os.environ.setdefault("DATABASE_URL", TEST_DB.replace("postgresql://", "postgresql+psycopg://"))
_os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "canonical-test")
_os.environ.setdefault("FLASHSHARE_GOKAPI_URL", "http://127.0.0.1:53901")
_os.environ.setdefault("FLASHSHARE_GOKAPI_KEY", "test-gk-key")

FAIL = []
def check(name, ok, detail=""):
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:160]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import base64
import http.client as _hc
import json
import urllib.parse
import uuid

from database import db

_HOST = ("127.0.0.1", int(_TEST_PORT))


def _raw(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=60)
    payload = body
    if isinstance(body, (dict, list)):
        payload = json.dumps(body).encode()
    elif isinstance(body, str):
        payload = body.encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    return r.status, hdrs, data


def _json(data):
    try:
        return json.loads(data)
    except Exception:
        return {}


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _session(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    st, h, _ = _raw("POST", "/auth/register", b, {"Content-Type": ct})
    if st not in (200, 302, 303):
        raise SystemExit(f"register failed: {st}")
    ck = (h.get("set-cookie") or "").split(";")[0]
    if not ck:
        raise SystemExit("no session cookie")
    return ck


def _multipart(fn, content, ct, fields):
    b = "----canon"
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in fields.items()]
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{fn}"\r\n'
                 f'Content-Type: {ct}\r\n\r\n'.encode("utf-8") + content + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


def _share(ck, fn, content, ct):
    body, mct = _multipart(fn, content, ct, {
        "expiryDays": "1", "allowedDownloads": "50",
        "client_ref": uuid.uuid4().hex})
    st, _, data = _raw("POST", "/api/uploads", body,
                       {"Content-Type": mct, "Cookie": ck})
    return st, _json(data)


def _row(public_id):
    with db() as con:
        return con.execute(
            "select * from uploads where public_id=%s", (public_id,)).fetchone()


def _gokapi_meta(gid):
    import urllib.request as _ur
    base = _os.environ["FLASHSHARE_GOKAPI_URL"].rstrip("/")
    req = _ur.Request(f"{base}/api/files/list/{urllib.parse.quote(gid)}",
                      headers={"apikey": _os.environ["FLASHSHARE_GOKAPI_KEY"]})
    with _ur.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"  # no trailing newline: the fake multipart parser strips it
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>'

ck = _session("sca" + _os.urandom(3).hex())

# ══ 1. declared MIME never becomes the stored fact ══
st, r = _share(ck, "x.tiff", SVG, "image/svg+xml; charset=utf-8")
f = r.get("file") or {}
pid_tiff = f.get("publicId")
check("1a .tiff with declared svg MIME lands (ext-authoritative)", st == 200 and pid_tiff, f"{st} {r}")
check("1b response contentType = canonical ext MIME", f.get("contentType") == "image/tiff", f)
row = _row(pid_tiff) if pid_tiff else None
check("1c stored content_type = image/tiff", row and row["content_type"] == "image/tiff",
      row and row["content_type"])
if row:
    check("1d storage object MIME = canonical", _gokapi_meta(row["gokapi_id"]).get("ContentType") == "image/tiff")
st, h, _ = _raw("GET", f"/f/{pid_tiff}/inline")
check("1e /inline refuses non-allowlisted type", st == 400, st)

# legacy dirty row: an old declared MIME stays refused (defence in depth)
with db() as con:
    con.execute("update uploads set content_type=%s where public_id=%s",
                ("image/svg+xml; charset=utf-8", pid_tiff))
st, h, _ = _raw("GET", f"/f/{pid_tiff}/inline")
check("1f dirty 'image/svg+xml; …' row refused by /inline", st == 400, st)
st, h, data = _raw("GET", f"/f/{pid_tiff}")
check("1g detail page does not embed dirty-typed file",
      st == 200 and f"/f/{pid_tiff}/inline".encode() not in data, st)

st, r = _share(ck, "p.png", PNG, "application/x-evil")
pid_png = (r.get("file") or {}).get("publicId")
check("1h png with bogus declared MIME stored as image/png",
      st == 200 and (r.get("file") or {}).get("contentType") == "image/png", r)
st, h, data = _raw("GET", f"/f/{pid_png}/inline")
check("1i /inline serves allowlisted image with exact MIME",
      st == 200 and h.get("content-type") == "image/png" and data == PNG,
      f"{st} {h.get('content-type')}")
check("1j inline disposition carries filename*",
      h.get("content-disposition", "").startswith("inline;")
      and "filename*=UTF-8''p.png" in h.get("content-disposition", ""), h.get("content-disposition"))

# ══ 2. non-ASCII names never break response headers ══
st, r = _share(ck, "报告.pdf", PDF, "application/pdf")
pid_pdf = (r.get("file") or {}).get("publicId")
check("2a non-ASCII pdf share lands", st == 200 and pid_pdf, f"{st} {r}")
enc = urllib.parse.quote("报告.pdf", safe="")
st, h, _ = _raw("GET", f"/f/{pid_pdf}?download=1")
check("2b redirect download: 302 + RFC 5987 name",
      st == 302 and f"filename*=UTF-8''{enc}" in h.get("content-disposition", ""),
      f"{st} {h.get('content-disposition')}")
with db() as con:
    con.execute("update uploads set url_hotlink='' where public_id=%s", (pid_pdf,))
st, h, data = _raw("GET", f"/f/{pid_pdf}?download=1")
check("2c streamed fallback download: 200 (no latin-1 crash)",
      st == 200 and data == PDF, st)
check("2d streamed fallback: attachment + filename* + canonical MIME",
      h.get("content-disposition", "").startswith("attachment;")
      and f"filename*=UTF-8''{enc}" in h.get("content-disposition", "")
      and h.get("content-type", "").startswith("application/pdf"),
      f"{h.get('content-disposition')} {h.get('content-type')}")
st, h, data = _raw("GET", f"/f/{pid_pdf}/print")
check("2e print view for pdf renders", st == 200, st)

st, _, data = _raw("POST", "/api/tokens", {"name": "sca"},
                   {"Content-Type": "application/json", "Cookie": ck})
tok = (_json(data).get("token") or {}).get("token")
check("2f api token minted", st == 200 and tok, st)
st, h, data = _raw("GET", f"/api/v1/uploads/{pid_pdf}", headers={"Authorization": f"Bearer {tok}"})
check("2g REST fetch with non-ASCII name: 200 + filename*",
      st == 200 and data == PDF and f"filename*=UTF-8''{enc}" in h.get("content-disposition", ""),
      f"{st} {h.get('content-disposition')}")

# ══ 3. truncation keeps the extension ══
long_name = "a" * 130 + ".pdf"
st, r = _share(ck, long_name, PDF, "application/pdf")
name = (r.get("file") or {}).get("filename") or ""
check("3a >120-char name accepted", st == 200, f"{st} {r}")
check("3b truncated name keeps .pdf and ≤120 chars",
      name.endswith(".pdf") and len(name) <= 120, name)

# ══ 4. reflected market query is escaped ══
q = '"><script>alert(1)</script>'
st, h, data = _raw("GET", "/tools?q=" + urllib.parse.quote(q))
check("4a /tools?q reflects no raw markup",
      st == 200 and b"<script>alert(1)</script>" not in data and b'"><script' not in data, st)

# ══ 5. unit boundaries: sniff authority / remote names / storage headers ══
from security import sniff_allowed
ok, t = sniff_allowed("a.svg", "text/html", b"<svg/>")
check("5a sniff returns canonical MIME for ext, ignores declared", ok and t == "image/svg+xml", t)
ok, t = sniff_allowed("a.ico", "", b"\x00\x00\x01\x00")
check("5b multi-MIME ext resolves deterministically", ok and t == "image/x-icon", t)

import remote as _remote
out = _remote._wrap_raw({}, PNG, 'evil\r\nContent-Type: text/html\r\n"x.png')
check("5c endpoint filename sanitized (no CR/LF/quotes), ext kept",
      not any(c in out["filename"] for c in '\r\n"') and out["filename"].endswith(".png"),
      repr(out["filename"]))
check("5d endpoint output MIME = canonical ext MIME", out["content_type"] == "image/png", out["content_type"])

import io
from gokapi import upload_to_gokapi
try:
    upload_to_gokapi(io.BytesIO(PNG), "a\r\nb.png", "image/png", 1, 1, len(PNG))
    check("5e storage multipart refuses CR/LF in part headers", False, "no error")
except ValueError as exc:
    check("5e storage multipart refuses CR/LF in part headers", str(exc) == "gokapi_header_injection", exc)

from utils import sanitize_filename, content_disposition
check("5f sanitize keeps ext on long names",
      sanitize_filename("b" * 200 + ".docx").endswith(".docx")
      and len(sanitize_filename("b" * 200 + ".docx")) == 120)
cd = content_disposition("attachment", 'a"b\\c\r\n.pdf')
check("5g disposition fallback strips quotes/backslash/control chars",
      cd.startswith('attachment; filename="abc.pdf";'), cd)

print(f"\n{'FAIL' if FAIL else 'PASS'}: {len(FAIL)} failure(s)")
_sys.exit(1 if FAIL else 0)
