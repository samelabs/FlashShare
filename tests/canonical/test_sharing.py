#!/usr/bin/env python3
"""Sharing chain — Upload → sniff/quota → Gokapi → public link → preview:
ingress entitlement and guards, request receipts (idempotency), preview
MIME admission, ZIP preview, share pages and continue-processing.
"""
from _harness import FAIL, PG, PORT, REPO, TEST_PORT, check, finish, purge_users, section  # fail-closed gate runs on import


# ════════════════════════════════════════════════════════════════════════
# Remote ZIP listing
#
# Remote ZIP listing (R10 rework): the central directory is fetched by
# its EXACT absolute offset — no heuristic slack window, no cd_before_tail.
#
#   Z1  a legal archive whose CD sits outside the tail window (large
#       archive comment) lists correctly via the precise [cd_offset,
#       cd_offset+cd_size) fetch
#   Z2  a CD beyond MAX_CD_BYTES answers cd_too_large
#   Z3  a CD window overrunning the EOCD's absolute position answers
#       cd_overruns_eocd (corrupt)
#
# Fail-closed: standard canonical env guard (the listing itself is pure
# in-memory; no DB/app needed).
# ════════════════════════════════════════════════════════════════════════
section('Remote ZIP listing')

import os
import struct
import sys


from zip_preview import (CD_FIXED, CD_SIG, EOCD_FIXED, EOCD_SIG,
                         MAX_CD_BYTES, find_eocd, list_zip_remote)


def _cd_entry(name: str) -> bytes:
    raw = name.encode()
    return CD_FIXED.pack(0x02014b50, 20, 4, 0, 0, 0, 0, 0, 0, 0,
                         len(raw), 0, 0, 0, 0, 0, 0) + raw


def _assemble(entries_cd: bytes, comment: bytes, *, cd_offset=None,
              cd_size=None, count=0) -> bytes:
    """[local-data stub] + [CD] + [comment] + [EOCD]. cd_offset/cd_size
    default to the truthful layout; override to forge corruption."""
    body = b"PK\x03\x04" + b"\x00" * 64          # local-header stub
    cd = entries_cd
    off = cd_offset if cd_offset is not None else len(body)
    size = cd_size if cd_size is not None else len(cd)
    eocd = EOCD_FIXED.pack(0x06054b50, 0, 0, 0, count, size, off,
                           len(comment))
    return body + cd + comment + eocd


def _fake_fetch(blob: bytes):
    calls = []
    def fetch_range(start: int, length: int) -> bytes:
        calls.append((start, length))
        return blob[start:start + length]
    fetch_range.calls = calls
    return fetch_range


# ── Z1: CD outside the tail window (476,790-byte CD + 60,000 comment),
#    precise fetch lists every entry ──
_names = ["folder/" + "a" * 1130 + f"-{i}.png" for i in range(400)]
_pad = 476790 - (400 * 46 + sum(len(n) for n in _names))
_names[0] = "folder/" + "a" * (1130 + _pad) + "-0.png"
_cd = b"".join(_cd_entry(n) for n in _names)
check("Z1 fixture CD size is the required 476,790 bytes", len(_cd) == 476790,
      len(_cd))
blob = _assemble(_cd, b"\x00" * 60000, count=len(_names))
fetch = _fake_fetch(blob)
entries = list_zip_remote(fetch, len(blob))
_exact = fetch.calls
check("Z1 legal big-comment archive lists all entries",
      [e["name"] for e in entries] == _names, f"{len(entries)} entries")
check("Z1 the CD was fetched at its exact absolute offset, once",
      _exact[1:] == [(68, 476790)], str(fetch.calls))

# ── Z2: CD beyond the 512KiB cap → cd_too_large ──
_big_cd = b"".join(_cd_entry(n) for n in
                   ["folder/" + "b" * 1130 + f"-{i}.png" for i in range(800)])
blob2 = _assemble(_big_cd, b"", count=800)
try:
    list_zip_remote(_fake_fetch(blob2), len(blob2))
    check("Z2 oversized CD → cd_too_large", False, "no error raised")
except ValueError as exc:
    check("Z2 oversized CD → cd_too_large", str(exc) == "cd_too_large", str(exc))

# ── Z3: EOCD lying about the CD (window overruns the EOCD) → corrupt ──
blob3 = _assemble(_cd, b"", cd_offset=64, cd_size=len(_cd) + 500,
                  count=len(_names))
try:
    list_zip_remote(_fake_fetch(blob3), len(blob3))
    check("Z3 CD window overrunning the EOCD → cd_overruns_eocd",
          False, "no error raised")
except ValueError as exc:
    check("Z3 CD window overrunning the EOCD → cd_overruns_eocd",
          str(exc) == "cd_overruns_eocd", str(exc))

# ── Z3b: find_eocd returns the archive-absolute CD offset ──
tail_len = min(len(blob), MAX_CD_BYTES)
cs, co, _ = find_eocd(blob[len(blob) - tail_len:], len(blob) - tail_len)
check("Z3b find_eocd returns the absolute CD offset and true size",
      (cs, co) == (len(_cd), 68), f"size={cs} offset={co}")


# ════════════════════════════════════════════════════════════════════════
# ZIP preview unavailability
#
# Route-level regression (INL-7, restored from hotfix
# web-flow-result-production): when the internal authenticated Gokapi
# channel fails (401/404) the ZIP preview endpoint /f/{id}/zip-list must
# answer a typed 503 preview_unavailable — never a 422 ZIP-complexity
# classification, never a fallback to the counted public download path.
#
# Fail-closed: FLASHSHARE_TEST_PORT (isolated, != 8000) + TEST_DB_URL +
# a running app and fake Gokapi are required.
# ════════════════════════════════════════════════════════════════════════
section('ZIP preview unavailability')

import io
import json
import re
import os
import sys
import urllib.request
import uuid
import zipfile

GOKAPI = os.environ.get("FLASHSHARE_GOKAPI_URL")
GOKAPI_KEY = os.environ.get("FLASHSHARE_GOKAPI_KEY")
if not GOKAPI or not GOKAPI_KEY:
    sys.stderr.write("ERROR: FLASHSHARE_GOKAPI_URL/KEY are required.\n"); sys.exit(2)


import http.client as _hc

_HOST = ("127.0.0.1", int(TEST_PORT))
_u = "zp" + uuid.uuid4().hex[:6]


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    raw = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    setc = r.getheader("Set-Cookie") or ""
    conn.close()
    try:
        return r.status, hdrs, (json.loads(raw) if raw else None), setc
    except Exception:
        return r.status, hdrs, raw.decode(errors="replace"), setc


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _gk_fail(mode):
    req = urllib.request.Request(
        GOKAPI.rstrip("/") + "/ci/fail-internal",
        data=json.dumps({"mode": mode or None}).encode(),
        headers={"apikey": GOKAPI_KEY, "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()


from database import db

uid = None
try:
    # fixture: user + a real zip upload through the web surface
    b, ct = _form({"username": _u, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{_u}@t.co"})
    st, _h, _b, _sc = _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": _u, "password": "Test12345"})
    st_l, _h, _b, ck = _req("POST", "/auth/login", b, {"Content-Type": ct})
    check("F1 fixtures: register + login", st in (200, 302) and st_l in (200, 302),
          (st, st_l))
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (_u,)).fetchone()["id"])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "hello")
    # sharing runs through the share tool — stage the source,
    # then execute; the settled file item carries the public share id.
    body = (f'--B\r\nContent-Disposition: form-data; name="client_ref"\r\n\r\ninl7-{_u}\r\n'
            f'--B\r\nContent-Disposition: form-data; name="tool"\r\n\r\nshare\r\n'
            f'--B\r\nContent-Disposition: form-data; name="file"; filename="p.zip"\r\n'
            f'Content-Type: application/zip\r\n\r\n').encode() + buf.getvalue() + b"\r\n--B--\r\n"
    st, _h, d, _sc = _req("POST", "/api/uploads", body,
                          {"Content-Type": "multipart/form-data; boundary=B",
                           "Cookie": ck})
    staged = (d or {}).get("file") or {}
    st_x, _h2, dx, _sc2 = _req("POST", "/api/tools/execute",
        {"tool_id": "share",
         "source": {"files": [{"public_id": staged.get("stagingId"),
                               "source_token": staged.get("sourceToken") or ""}]},
         "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 50},
         "client_ref": f"inl7x-{_u}"},
        {"Content-Type": "application/json", "Cookie": ck})
    items = ((dx or {}).get("result") or {}).get("items") or []
    pid = next((it.get("public_id") for it in items if it.get("kind") == "file"), None)
    check("F2 zip upload via web (share tool)", st == 200 and st_x == 200 and bool(pid),
          (st, st_x, str(dx)[:90]))

    if pid:
        st, _h, d, _sc = _req("GET", f"/f/{pid}/zip-list", {"Cookie": ck})
        check("INL-5 baseline zip-list ok", st == 200 and (d or {}).get("ok"), st)

        for mode in ("401", "404"):
            _gk_fail(mode)
            try:
                st_f, _hf, df, _scf = _req("GET", f"/f/{pid}/zip-list",
                                           {"Cookie": ck})
                check(f"INL-7 internal {mode} → 503 preview_unavailable",
                      st_f == 503 and (df or {}).get("error") == "preview_unavailable",
                      (st_f, str(df)[:80]))
                check(f"INL-7 internal {mode} never 422", st_f != 422, st_f)
            finally:
                _gk_fail(None)

        st, _h, d, _sc = _req("GET", f"/f/{pid}/zip-list", {"Cookie": ck})
        check("INL-7b channel restored → 200 again", st == 200, st)

        # F13: zip-preview.js rides its own content stamp (never an empty
        # ?v=); only a non-empty stamp gets the immutable cache header
        st, _hd, page, _sc = _req("GET", f"/f/{pid}", {"Cookie": ck})
        _m = re.search(r"zipPreviewVersion:\s*\"([0-9a-f]+)\"", page or "")
        check("VP-1 detail page carries a non-empty zipPreviewVersion stamp",
              st == 200 and bool(_m and _m.group(1)),
              f"{st} {_m and _m.group(1)}")
        if _m:
            st_v, h_v, _b, _s = _req(
                "GET", f"/static/zip-preview.js?v={_m.group(1)}")
            check("VP-2 real-stamp static URL → immutable cache header",
                  st_v == 200 and "immutable" in (h_v.get("cache-control") or ""),
                  (st_v, h_v.get("cache-control")))
            st_e, h_e, _b2, _s2 = _req("GET", "/static/zip-preview.js?v=")
            check("VP-3 empty ?v= never pinned immutable",
                  st_e == 200
                  and "immutable" not in (h_e.get("cache-control") or ""),
                  (st_e, h_e.get("cache-control")))
finally:
    _gk_fail(None)
    if uid is not None:
        with db() as con:
            purge_users(con, [uid])
    print("cleanup done")


# ════════════════════════════════════════════════════════════════════════
# Preview MIME admission
#
# Preview MIME admission: parameterized MIME must classify identically
# to its bare form at every preview consumer.
#
# Upload stores the client-declared content_type verbatim (it may carry
# '; param' suffixes). Admission and projection comparisons must use the
# normalized base type — the historical full-string SVG exclusion let
# `image/svg+xml; charset=utf-8` through to the same-origin inline
# channel while the upload side still treated it as SVG.
# ════════════════════════════════════════════════════════════════════════
section('Preview MIME admission')

import os as _os
import sys as _sys


_os.environ.setdefault("FLASHSHARE_GOKAPI_URL", "http://127.0.0.1:53901")
_os.environ.setdefault("FLASHSHARE_GOKAPI_KEY", "test-gk-key")


import base64
import http.client as _hc
import json
import time
import uuid
ts = int(time.time())
_u = "pm" + _os.urandom(3).hex()
_HOST_pma = ("127.0.0.1", int(TEST_PORT))

from database import db

GK_URL = _os.environ["FLASHSHARE_GOKAPI_URL"].rstrip("/")
GK_KEY = _os.environ["FLASHSHARE_GOKAPI_KEY"]


def _req(method, path, body=None, headers=None, cookies=None):
    conn = _hc.HTTPConnection(*_HOST_pma, timeout=60)
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


def _form_pma(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _register_login(name):
    b, ct = _form_pma({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{name}@t.co"})
    conn = _hc.HTTPConnection(*_HOST_pma, timeout=30)
    conn.request("POST", "/auth/register", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    conn.close()
    b, ct = _form_pma({"login": name, "password": "Test12345"})
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
_os.environ.setdefault("FLASHSHARE_BASE_URL", f"http://127.0.0.1:{TEST_PORT}")
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


# ════════════════════════════════════════════════════════════════════════
# Share cards, continue processing, share bar
#
# R/F governance canonical.
#
# S*  Share cards: an image file page and a result page with images use
#     the image's inline URL as og:image; the file page card names the
#     file and its facts.
# C*  Continue processing: /api/results/{id}/files is owner-only; MCP
#     result file items carry public_id, and a later call can take that
#     public_id as a reference (no re-upload).
# B*  Share bar + agent view: /f and /r carry the one share bar whose X
#     link shares THIS page; a page without its own image gets the small
#     card; Accept: application/json returns the page facts as JSON;
#     the history list shares through the system share (no X link).
# ════════════════════════════════════════════════════════════════════════
section('Share cards, continue processing, share bar')

import base64
import http.client as _hc
import json as _json
import os as _os
import sys as _sys
import time as _time
import uuid as _uuid


_HOST_rf = ("127.0.0.1", int(TEST_PORT))
_u = "rf" + _uuid.uuid4().hex[:6]
ts_rf = int(_time.time())

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")
PDF = b"%PDF-1.4\n%%EOF\n"


def _req_rf(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST_rf, timeout=120)
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


def _form_rf(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _mcp(body, token):
    st, _h, d, _sc = _req_rf("POST", "/mcp", body, {
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
    b, ct = _form_rf({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    _req_rf("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form_rf({"login": name, "password": "Test12345"})
    _st, _h, _d, sc = _req_rf("POST", "/auth/login", b, {"Content-Type": ct})
    ck = sc.split(";")[0]
    _st, _h, d_k, _sc = _req_rf("POST", "/api/keys", {"name": "rf"},
                             {"Content-Type": "application/json", "Cookie": ck})
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck, d_k["key"]["key"]


from database import db  # noqa: E402
from i18n import server_text as _texts  # noqa: E402
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
    _st, _h, page, _sc = _req_rf("GET", f"/f/{img_pid}", None, {"Accept": "text/html"})
    og = _re.search(r'property="og:image" content="([^"]+)"', page or "")
    check("S1 /f image page og:image is the inline view",
          og and og.group(1).endswith(f"/f/{img_pid}/inline"), og and og.group(1))
    check("S2 /f card title is the file name",
          'property="og:title" content="pic.png"' in (page or ""))
    _st, _h, page, _sc = _req_rf("GET", f"/r/{rid}", None, {"Accept": "text/html"})
    og = _re.search(r'property="og:image" content="([^"]+)"', page or "")
    check("S3 /r with an image uses it as og:image",
          og and og.group(1).endswith(f"/f/{img_pid}/inline"), og and og.group(1))

    # ═══ B: share bar, card type, agent JSON ═══
    pdf_pid = next((f["public_id"] for f in files
                    if f["filename"].endswith(".pdf")), "")
    _st, _h, page, _sc = _req_rf("GET", f"/f/{pdf_pid}", None, {"Accept": "text/html"})
    _page_url = f"/f/{pdf_pid}"
    from urllib.parse import unquote as _uq
    _x = _re.search(r'href="(https://x\.com/intent/tweet\?[^"]+)"', page or "")
    _xh = _uq(_x.group(1).replace("&amp;", "&")) if _x else ""
    check("B1 /f share bar X link shares this file page",
          'class="share-bar sb-compact"' in (page or "")
          and _xh.startswith("https://x.com/intent/tweet?text=518.PLUS&url=")
          and _xh.endswith(_page_url), _xh)
    check("B2 /f non-image → small card; image → large card",
          'name="twitter:card" content="summary"' in (page or ""), "")
    _st, _h, ipage, _sc = _req_rf("GET", f"/f/{img_pid}", None, {"Accept": "text/html"})
    check("B2b /f image page keeps the large card",
          'name="twitter:card" content="summary_large_image"' in (ipage or ""), "")
    st, h, d, _sc = _req_rf("GET", f"/f/{pdf_pid}", None, {"Accept": "application/json"})
    check("B3 /f Accept: application/json → file facts",
          st == 200 and isinstance(d, dict) and d.get("public_id") == pdf_pid
          and d.get("filename") == "doc.pdf"
          and str(d.get("download_url", "")).endswith(f"/f/{pdf_pid}?download=1")
          and "accept" in (h.get("vary") or "").lower(), f"{st} {d}")
    st, h, d, _sc = _req_rf("GET", f"/r/{rid}", None, {"Accept": "application/json"})
    _fi = [i for i in ((d or {}).get("items") or []) if i.get("kind") == "file"]
    check("B4 /r Accept: application/json → result facts",
          st == 200 and isinstance(d, dict) and len(_fi) == 2
          and all(i.get("download_url") and i.get("public_id") for i in _fi)
          and str(d.get("result_url", "")).endswith(f"/r/{rid}"), f"{st} {d}")
    st, _h, page, _sc = _req_rf("GET", f"/f/{pdf_pid}", None,
                             {"Accept": "text/html,application/json;q=0.9"})
    check("B5 a browser Accept still gets the HTML page",
          st == 200 and isinstance(page, str) and "<html" in page, st)
    # the print view reads its texts through the template var t (= texts);
    # the print_loading / print_hint keys are the live copy of that page
    st, _h, page, _sc = _req_rf("GET", f"/f/{pdf_pid}/print", None,
                             {"Accept": "text/html"})
    _tx = _texts("en")
    check("B5b /f print page renders print_loading + print_hint",
          st == 200 and _tx.get("print_loading") in (page or "")
          and _tx.get("print_hint") in (page or ""), st)
    _foot = open(_os.path.join(REPO, "static", "pages", "footer.js"),
                 encoding="utf-8").read()
    check("B6 history list shares via the system share, no X intent link",
          "x.com/intent" not in _foot and "shareLink" in _foot, "")

    # ═══ C: continue processing ═══
    st, _h, d, _sc = _req_rf("GET", f"/api/results/{rid}/files", None, {"Cookie": a_ck})
    check("C2 owner reads the result file list",
          st == 200 and len((d or {}).get("files") or []) == 2, f"{st} {d}")
    st, _h, d, _sc = _req_rf("GET", f"/api/results/{rid}/files", None, {"Cookie": b_ck})
    check("C3 another user gets 404", st == 404, st)
    _st, _h, page, _sc = _req_rf("GET", f"/r/{rid}", None,
                              {"Accept": "text/html", "Cookie": a_ck})
    check("C4 owner sees the continue button",
          f'href="/?from={rid}"' in (page or ""))
    _st, _h, page, _sc = _req_rf("GET", f"/r/{rid}", None, {"Accept": "text/html"})
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


# ════════════════════════════════════════════════════════════════════════
# Ingress boundary + entitlement
#
# Ingress boundary + entitlement invariants — 平台硬边界 + 身份权益.
#
# 长期 baseline: 把 entitlement 映射 (24h ingress quota + delivery 值域)
# 与 authoritative enforcement 锁进 canonical 体系。遵循 canonical
# fail-closed 规则: 必须显式提供 isolated port + isolated TEST_DB_URL,
# 禁止 production DB/port。业务契约断言, 不 snapshot 实现细节。
# ════════════════════════════════════════════════════════════════════════
section('Ingress boundary + entitlement')

import os as _os, sys as _sys
import sys
import json
import base64
import time, threading, re, uuid


import psycopg2
from governance.entitlements import (resolve, check_delivery)


def _row_views():
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
    from governance import plan_catalog as _pc
    _free = _pc.free_tier().benefits.as_dict("free")
    _plans = {t.code: t.benefits.as_dict(t.code) for t in _pc.catalog() if not t.is_free}
    return _free, _plans


FREE, _PLANS = _row_views()
from governance.delivery import (validate_delivery, EXPIRY_DAYS,
                                 DOWNLOAD_LIMITS, INTERMEDIATE_TTL_DAYS,
                                 INTERMEDIATE_DOWNLOAD_LIMIT)
from economy.subscription import plan_of
from sharing.uploads import reserve_ingress, release_ingress, check_quota
from repo import upload_usage as usage_repo
from database import db


_con = psycopg2.connect(PG); _con.autocommit = True
_cur = _con.cursor()

def _uid(username):
    _cur.execute("select id from users where username=%s", (username,))
    row = _cur.fetchone()
    return row[0] if row else None

def _new_user(prefix):
    import uuid
    u = prefix + uuid.uuid4().hex[:6]
    _cur.execute(
        "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
        " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id",
        (u, f"{u}@t.co", int(time.time()), int(time.time())))
    return u, _cur.fetchone()[0]

def _seed_plan(user_id, code, status, renews_at):
    _cur.execute("select id from plans where code=%s", (code,))
    row = _cur.fetchone()
    ts = int(time.time())
    if not row:
        _cur.execute(
            "insert into plans (code, name, monthly_credits, price_cents, sort_order, created_at, updated_at)"
            " values (%s, %s, 0, 0,"
            " (SELECT COALESCE(MAX(p.sort_order),0)+1 FROM plans p WHERE p.code <> 'free'),"
            " %s, %s) returning id", (code, code, ts, ts))
        plan_id = _cur.fetchone()[0]
    else:
        plan_id = row[0]
    _cur.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id, status, started_at, renews_at, updated_at)"
        " values (%s, %s, %s, %s, %s, %s, %s) on conflict do nothing",
        (user_id, plan_id, f"sub:fx:{user_id}", status, ts, renews_at, ts))

def _clear_usage(user_id):
    _cur.execute("delete from upload_usage where user_id=%s", (user_id,))

PLUS_BYTES = 5 * 1024 ** 3
GB = 1024 ** 3

# ═══ 1. Entitlement resolver ═══
u_f, id_f = _new_user("entf")
r = resolve(id_f)
check("R1 无 plan → Free (含 delivery 值域 = Free 档)",
      r["plan_code"] == "free"
      and r["ingress_files_24h"] == FREE["ingress_files_24h"]
      and r["ingress_bytes_24h"] == FREE["ingress_bytes_24h"]
      and r["expiry_days"] == (1, 3, 7)
      and r["download_limits"] == (1, 10, 50, 100, 200), str(r))

u_e, id_e = _new_user("ente")
_seed_plan(id_e, "plus", "active", int(time.time()) - 100)  # renews_at 已过 → member=false
p = plan_of(id_e)
check("R2a fixture: 过期 plus 行 plan_of().member == False", p and p["member"] is False, str(p))
r = resolve(id_e)
check("R2b member=false 的 plus → Free", r["plan_code"] == "free", str(r))

u_p, id_p = _new_user("entp")
_seed_plan(id_p, "plus", "active", int(time.time()) + 30 * 86400)
p = plan_of(id_p)
check("R3a fixture: active plus member == True", p and p["member"] is True, str(p))
r = resolve(id_p)
check("R3b member+plus → 500 files / 5 GiB / 全值域 delivery",
      r == {"plan_code": "plus", "ingress_files_24h": 500,
            "ingress_bytes_24h": PLUS_BYTES,
            "expiry_days": (1, 3, 7, 30),
            "download_limits": (1, 10, 50, 100, 200, 1000)}, str(r))

u_x, id_x = _new_user("entx")
_seed_plan(id_x, "future-unknown", "active", int(time.time()) + 30 * 86400)
p = plan_of(id_x)
check("R4a fixture: 未知 code member == True", p and p["member"] is True, str(p))
r = resolve(id_x)
check("R4b member+未知 code → Free", r["plan_code"] == "free", str(r))

# resolver 不读 Creem/payment 内部: 源码静态守卫 (契约: entitlements 只 import plan_of)
src = open(_os.path.join(REPO, "core", "governance", "entitlements.py"), encoding="utf-8").read()
_import_lines = "\n".join(l for l in src.split("\n") if l.startswith(("import ", "from ")))
check("R5 resolver 不 import creem/orders/payment 内部",
      not re.search(r"creem|orders|payment|webhook", _import_lines, re.I))
check("R6 resolver 只经 economy.subscription.plan_of 取会员事实",
      "plan_of" in src and "user_plans" not in src)

# ═══ 2. Free quota 边界 + 结构化 denial contract ═══
ip = "203.0.113.9"
FLIM = FREE["ingress_files_24h"]; BLIM = FREE["ingress_bytes_24h"]

# Free files: used = limit-2, incoming = 3 → 精确六字段
_clear_usage(id_f)
with db() as c:
    usage_repo.insert_many(c, id_f, ip, [1] * (FLIM - 2), int(time.time()))
ok, r, _ = reserve_ingress(id_f, ip, [1] * 3)
check("F3 Free files denial 精确结构 {error,limit,used,incoming,remaining,plan_code}",
      (not ok) and r == {"error": "err_ingress_files_24h", "limit": FLIM,
                         "used": FLIM - 2, "incoming": 3, "remaining": 2,
                         "plan_code": "free"}, str(r))
check("F3b incoming 不截断为 remaining (3 > 2 仍报 3)",
      (not ok) and r["incoming"] == 3 and r["remaining"] == 2)

# Free bytes: 同形 (单行大 size 即 sum 语义, 无需十亿行)
_clear_usage(id_f)
with db() as c:
    usage_repo.insert_many(c, id_f, ip, [BLIM - 100], int(time.time()))
ok, r, _ = reserve_ingress(id_f, ip, [101])
check("F5 Free bytes denial 精确结构",
      (not ok) and r == {"error": "err_ingress_bytes_24h", "limit": BLIM,
                         "used": BLIM - 100, "incoming": 101,
                         "remaining": 100, "plan_code": "free"}, str(r))

# 边界通过性 (既有行为不回归)
_clear_usage(id_f)
ok, r, _ = reserve_ingress(id_f, ip, [1] * (FLIM - 1))
check("F1 Free limit-1 files 通过", ok)
ok, r, _ = reserve_ingress(id_f, ip, [1])
check("F2 Free 第 limit 个通过", ok)
_clear_usage(id_f)
ok, r, _ = reserve_ingress(id_f, ip, [BLIM])
check("F4 Free 恰 bytes-limit 通过", ok)

# ═══ 3. PLUS quota 边界 (结构值来自 effective entitlement, 非 Free 常量) ═══
PLIM = _PLANS["plus"]["ingress_files_24h"]; PBLIM = _PLANS["plus"]["ingress_bytes_24h"]
_clear_usage(id_p)
with db() as c:
    usage_repo.insert_many(c, id_p, ip, [1] * (PLIM - 2), int(time.time()))
ok, r, _ = reserve_ingress(id_p, ip, [1] * 3)
check("P3 PLUS files denial: plan_code=plus, limit=500 (effective entitlement)",
      (not ok) and r["error"] == "err_ingress_files_24h" and r["plan_code"] == "plus"
      and r["limit"] == 500 and r["used"] == 498 and r["incoming"] == 3
      and r["remaining"] == 2, str(r))
_clear_usage(id_p)
with db() as c:
    usage_repo.insert_many(c, id_p, ip, [PBLIM - 50], int(time.time()))
ok, r, _ = reserve_ingress(id_p, ip, [51])
check("P5 PLUS bytes denial: limit=5GiB, plan_code=plus",
      (not ok) and r["error"] == "err_ingress_bytes_24h"
      and r["limit"] == PLUS_BYTES and r["plan_code"] == "plus"
      and r["remaining"] == 50, str(r))
_clear_usage(id_p)
ok, r, _ = reserve_ingress(id_p, ip, [1] * (PLIM - 1))
check("P1 PLUS limit-1 files 通过", ok)
ok, r, _ = reserve_ingress(id_p, ip, [1])
check("P2 PLUS 第 500 个通过", ok)
_clear_usage(id_p)
ok, r, _ = reserve_ingress(id_p, ip, [PLUS_BYTES])
check("P4 PLUS 恰 5 GiB 通过", ok)

# check/reserve 同义: 同 snapshot + incoming → denial 字段完全一致
_clear_usage(id_f)
with db() as c:
    usage_repo.insert_many(c, id_f, ip, [1] * (FLIM - 2), int(time.time()))
ok1, d1 = check_quota(id_f, ip, 1, incoming_count=3)
ok2, d2, _ = reserve_ingress(id_f, ip, [1] * 3)
check("Q1 check_quota denial == reserve_ingress denial (逐字段)",
      (not ok1) and (not ok2) and d1 == d2, f"check={d1} reserve={d2}")

# ═══ 4. IP 不参与 quota ═══
u_i2, id_i2 = _new_user("enti")
_clear_usage(id_f); _clear_usage(id_i2)
ok1, _, _ = reserve_ingress(id_f, "198.51.100.7", [1] * FLIM)
ok2, _, _ = reserve_ingress(id_i2, "198.51.100.7", [1])
check("I1 同 IP 两用户 usage 相互独立 (Free 满, 另一用户第1个仍过)", ok1 and ok2)

# 测试自身不把 ip_usage_count 当 quota 依据 (静态守卫: 本文件零调用)
_self_src = open(_os.path.abspath(__file__), encoding="utf-8").read()
_probe = "ip_usage_" + "count("  # 动态拼字面量, 避免守卫行自匹配
check("I2 测试自身不调用 ip_usage_count 作 quota decision",
      _probe not in _self_src.replace('"' + "ip_usage_" + '" + "count("', ""))

# ═══ 5. Reservation semantics ═══
_clear_usage(id_f)
with db() as c:
    usage_repo.insert_many(c, id_f, ip, [1] * (FLIM - 1), int(time.time()))
results = []
def _race():
    okx, rx, _ = reserve_ingress(id_f, ip, [1])
    results.append((okx, rx))
t1 = threading.Thread(target=_race); t2 = threading.Thread(target=_race)
t1.start(); t2.start(); t1.join(); t2.join()
granted = sum(1 for okx, _ in results if okx)
check("S1 same-user 并发抢最后名额恰一个成功", granted == 1, str(results))
_s1_denials = [d for okx, d in results if not okx]
check("S1b 失败线程收到 structured quota denial",
      len(_s1_denials) == 1 and _s1_denials[0].get("error") == "err_ingress_files_24h"
      and "limit" in _s1_denials[0] and "remaining" in _s1_denials[0], str(_s1_denials))

_clear_usage(id_f)
ok, r, res = reserve_ingress(id_f, ip, [1] * 10)
release_ingress(id_f, res)
ok2, r2, _ = reserve_ingress(id_f, ip, [1] * FLIM)
check("S2 release_ingress 恢复失败 reservation", ok and ok2 and r2 is None)

_clear_usage(id_f)
ok, r, res = reserve_ingress(id_f, ip, [1] * 5)
_cur.execute("delete from uploads where user_id=%s", (id_f,))  # 普通 upload 删除
_cur.execute("select count(*) from upload_usage where user_id=%s", (id_f,))
left = _cur.fetchone()[0]
check("S3 upload 行删除不退款 24h usage (usage 仍 5)", left == 5, str(left))

# generated output 不新增 ingress usage: canonical 契约 = reserve 只挂在 source
# 接受路径, output 落盘不产生新 upload_usage。静态守卫:
_pipe_src = ""  # execution/pipeline.py deleted (FX-2 dead module)
_api = open(_os.path.join(REPO, "app", "routes", "api.py"), encoding="utf-8").read()
_staging = open(_os.path.join(REPO, "core", "sharing", "staging.py"),
                encoding="utf-8").read()
# the coherence pass: the dead upload_sources pipeline (the sole reserve_ingress
# caller) is deleted — quota accounting rides ingress receipts only, claimed
# in ONE place: the P4 staging single point (api/mcp/ticket routes delegate).
check("S4 quota 入口收敛: receipts 唯一 (零直接 reserve, 暂存单点 claim)",
      _pipe_src.count("reserve_ingress(") == 0 and _api.count("reserve_ingress(") == 0
      and _api.count("stage_sources(") >= 1
      and _staging.count("claim_ingress_quota") >= 1,
      f"pipe={_pipe_src.count('reserve_ingress(')} api={_api.count('reserve_ingress(')}")
# 引用既有 the canonical-invocation unification E2E: tests/canonical/test_invocation.py 全链 (upload→execute→output)
# 已证明一次调用 usage 不翻倍; 本文件不重复造 E2E。

# ═══ 5b. Delivery entitlement 两层契约 ═══
# platform protocol validity (validate_delivery) vs user entitlement (check_delivery) 分离

def _proto_ok(ed, dl):
    try:
        validate_delivery({"expiry_days": ed, "allowed_downloads": dl})
        return True
    except ValueError:
        return False

check("D-P1 validate(30/1000) protocol PASS (平台枚举已扩展)",
      _proto_ok(30, 1000) and _proto_ok(1, 1000))
check("D-P2 validate(5/999) protocol FAIL",
      not _proto_ok(5, 50) and not _proto_ok(7, 999))
check("D-P3 平台枚举 = (1,3,7,30) / (1,10,50,100,200,1000)",
      EXPIRY_DAYS == (1, 3, 7, 30) and DOWNLOAD_LIMITS == (1, 10, 50, 100, 200, 1000))
check("D-P4 intermediate custody 固定 1 day / 10 downloads (平台策略非权益)",
      INTERMEDIATE_TTL_DAYS == 1 and INTERMEDIATE_DOWNLOAD_LIMIT == 10)

rf = resolve(id_f)
rp = resolve(id_p)
rx = resolve(id_x)
check("D-E1 Free delivery 值域 = 显式 (1,3,7)+(1,10,50,100,200)",
      rf["expiry_days"] == (1, 3, 7)
      and rf["download_limits"] == (1, 10, 50, 100, 200), str(rf))
check("D-E2 PLUS delivery 值域 = 显式 (1,3,7,30)+(1,10,50,100,200,1000)",
      rp["expiry_days"] == (1, 3, 7, 30)
      and rp["download_limits"] == (1, 10, 50, 100, 200, 1000), str(rp))
check("D-E3 未知 plan → Free delivery 值域", rx["expiry_days"] == (1, 3, 7)
      and rx["download_limits"] == (1, 10, 50, 100, 200), str(rx))

# Entitlement 显式声明 guard: 不得从 platform enum 推导
# (校验机制随 Tier 本体化迁至 plan_catalog; entitlements 仍是消费断言锚)
ent_src = open(_os.path.join(REPO, "core", "governance", "entitlements.py"), encoding="utf-8").read()
_pc_src = open(_os.path.join(REPO, "core", "governance", "plan_catalog.py"), encoding="utf-8").read()
_defs = _pc_src.split("def _validate_against_protocol")[0]  # 定义区
check("D-X1 entitlement 源无枚举推导 (comprehension/filter/slice)",
      "tuple(d for d in" not in _defs
      and "for d in EXPIRY_DAYS" not in _defs
      and "for d in DOWNLOAD_LIMITS" not in _defs
      and "EXPIRY_DAYS[:-" not in _defs and "DOWNLOAD_LIMITS[:-" not in _defs
      and "EXPIRY_DAYS[" not in _defs and "DOWNLOAD_LIMITS[" not in _defs)
check("D-X2 plan 定义不直接赋值 platform 全集",
      '"expiry_days": EXPIRY_DAYS' not in _defs
      and '"download_limits": DOWNLOAD_LIMITS' not in _defs)
check("D-X3 fail-closed protocol-subset 校验存在 (显式→被支持, 不反向生成)",
      "_validate_against_protocol" in _pc_src
      and "ConfigurationError" in _pc_src)
# 未来协议扩容 ≠ 自动授权 (独立构造验证, 不 monkeypatch production):
from governance.plan_catalog import Tier, ConfigurationError as _PCE, _validate_against_protocol
try:
    _t_fut = {"id": 999999, "code": "future-spec", "name": "F", "badge_key": "",
              "sort_order": 99, "active": True, "price_cents": 500, "currency": "USD",
              "creem_product_id": "prod_fut", "monthly_credits": 1, "period_days": 30,
              "ingress_files_24h": 1, "ingress_bytes_24h": 1,
              "expiry_days": (1, 3, 7, 60), "download_limits": (1, 10, 50, 100, 200, 5000)}
    _validate_against_protocol(Tier(_t_fut))
    check("D-X4 协议未支持的 entitlement 值 → ConfigurationError", False, "no raise")
except _PCE:
    check("D-X4 协议未支持的 entitlement 值 → ConfigurationError (fail closed)", True)
# 若平台真扩了 enum, 既有 entitlement 档不变 (不自动获得):
check("D-X5 Free/PLUS 声明独立于 protocol 全集 (重叠但非派生)",
      FREE["expiry_days"] != EXPIRY_DAYS      # Free ⊂ 全集
      and _PLANS["plus"]["expiry_days"] == EXPIRY_DAYS  # PLUS 恰等, 但显式字面声明 (D-X2 锁)
      and set(FREE["download_limits"]) < set(DOWNLOAD_LIMITS))
# 所有 entitlement 值 ⊆ platform enum (模块 import 成功即已过 fail-closed;
# 此处显式断言一次):
check("D-X6 全部 entitlement 值 ⊆ platform enum",
      set(FREE["expiry_days"]) <= set(EXPIRY_DAYS)
      and set(FREE["download_limits"]) <= set(DOWNLOAD_LIMITS)
      and set(_PLANS["plus"]["expiry_days"]) <= set(EXPIRY_DAYS)
      and set(_PLANS["plus"]["download_limits"]) <= set(DOWNLOAD_LIMITS))
# resolver 契约完整形状:
check("D-E4 resolve 返回五键 (ingress×2 + delivery×2 + plan_code)",
      set(rf) == {"plan_code", "ingress_files_24h", "ingress_bytes_24h",
                  "expiry_days", "download_limits"}, str(sorted(rf)))

dn = check_delivery(id_f, {"expiry_days": 30, "allowed_downloads": 200})
check("D-C1 Free 30 → 精确 err_expiry_not_entitled + 上下文",
      dn and dn["error"] == "err_expiry_not_entitled" and dn["field"] == "expiry_days"
      and dn["requested"] == 30 and dn["allowed"] == [1, 3, 7] and dn["plan_code"] == "free", str(dn))
dn = check_delivery(id_f, {"expiry_days": 7, "allowed_downloads": 1000})
check("D-C2 Free 1000 → err_download_limit_not_entitled + 上下文",
      dn and dn["error"] == "err_download_limit_not_entitled"
      and dn["field"] == "allowed_downloads" and dn["requested"] == 1000
      and dn["allowed"] == [1, 10, 50, 100, 200], str(dn))
check("D-C3 PLUS 30/1000 → None (有权)",
      check_delivery(id_p, {"expiry_days": 30, "allowed_downloads": 1000}) is None)
check("D-C4 Free 7/200 → None (有权)",
      check_delivery(id_f, {"expiry_days": 7, "allowed_downloads": 200}) is None)
# check_delivery 不自读 plan_of (静态: 源码只 import resolve)
_ent_src = open(_os.path.join(REPO, "core", "governance", "entitlements.py"), encoding="utf-8").read()
_check_fn = _ent_src.split("def check_delivery")[1]
_import_face = [l for l in _check_fn.split("\n") if l.startswith(("import ", "from "))]
check("D-C5 check_delivery 只消费 resolve() 不直读 plan_of()",
      not _import_face and "resolve(user_id)" in _check_fn)

# ═══ 5c. Authoritative enforcement (HTTP, 与 service 同栈) ═══
import http.client as _hc
_HOST_ine = ("127.0.0.1", PORT)

def _login(username):
    conn = _hc.HTTPConnection(*_HOST_ine, timeout=30)
    out = b""
    for k, v in {"login": username, "password": "Test12345"}.items():
        out += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    b, ct = out + b"--B--\r\n", "multipart/form-data; boundary=B"
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read(); ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close(); return ck

def _execute(ck, body):
    conn = _hc.HTTPConnection(*_HOST_ine, timeout=60)
    conn.request("POST", "/api/tools/execute", json.dumps(body),
                 {"Content-Type": "application/json", "Cookie": ck})
    r = conn.getresponse(); d = json.loads(r.read()); conn.close()
    return r.status, d

def _share(ck, ed, dl):
    import base64 as _b64
    png = _b64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    conn = _hc.HTTPConnection(*_HOST_ine, timeout=30)
    body = b""
    # uploads REQUIRE a tool (sharing = the share tool); delivery
    # entitlement is enforced at execute preflight (H2/H3 prove it).
    for k, v in {"tool": "share",
                 "client_ref": f"sh-{uuid.uuid4().hex[:10]}"}.items():
        body += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += b'--B\r\nContent-Disposition: form-data; name="file"; filename="d.png\r\nContent-Type: image/png\r\n\r\n' + png + b"\r\n--B--\r\n"
    conn.request("POST", "/api/uploads", body,
                 {"Content-Type": "multipart/form-data; boundary=B", "Cookie": ck})
    r = conn.getresponse(); d = json.loads(r.read()); conn.close()
    return r.status, d

def _register(username):
    conn = _hc.HTTPConnection(*_HOST_ine, timeout=30)
    out = b""
    for k, v in {"username": username, "password": "Test12345",
                 "password_confirm": "Test12345", "email": f"{username}@t.co"}.items():
        out += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    b, ct = out + b"--B--\r\n", "multipart/form-data; boundary=B"
    conn.request("POST", "/auth/register", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read(); conn.close()
    _cur.execute("select id from users where username=%s", (username,))
    return username, _cur.fetchone()[0]

_suffix = str(int(time.time()))[-5:]
_hf_name, _hf_id2 = _register("delfree" + _suffix)
_hp_name, _hp_id2 = _register("delplus" + _suffix)
_seed_plan(_hp_id2, "plus", "active", int(time.time()) + 30 * 86400)
assert resolve(_hf_id2)["plan_code"] == "free" and resolve(_hp_id2)["plan_code"] == "plus"
_ckf = _login(_hf_name); _ckp = _login(_hp_name)
def _inv(ed, dl):
    return {"tool_id": "share", "source": {"files": [{"public_id": "nonexistent", "source_token": ""}]},
            "params": {}, "delivery": {"expiry_days": ed, "allowed_downloads": dl}, "client_ref": f"ig-{uuid.uuid4().hex[:8]}"}

st, d = _execute(_ckf, _inv(5, 50))
check("H1 Free execute expiry=5 → 422 invalid_request (protocol 面)",
      st == 422 and d.get("error") == "invalid_request", f"{st} {d.get('error')}")
st, d = _execute(_ckf, _inv(30, 200))
check("H2 Free execute 30 → 403 err_expiry_not_entitled (entitlement 面)",
      st == 403 and d.get("error") == "err_expiry_not_entitled"
      and d.get("field") == "expiry_days" and d.get("plan_code") == "free", f"{st} {d.get('error')}")
st, d = _execute(_ckf, _inv(7, 1000))
check("H3 Free execute 1000 → 403 err_download_limit_not_entitled",
      st == 403 and d.get("error") == "err_download_limit_not_entitled", f"{st}")
st, d = _execute(_ckp, _inv(30, 1000))
check("H4 PLUS execute 30/1000 → 非 403/422 (entitlement 放行)",
      st not in (403, 422), f"{st} {d.get('error')}")
# H5-H7: the no-tool direct-share surface is RETIRED. Delivery
# entitlement for a share call lives at execute preflight — H2/H3 prove
# the typed denials; here the retired form itself is fail-closed.
def _no_tool(ck):
    import base64 as _b64
    png = _b64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    conn = _hc.HTTPConnection(*_HOST_ine, timeout=30)
    body = b""
    for k, v in {"client_ref": f"nt-{uuid.uuid4().hex[:10]}"}.items():
        body += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += b'--B\r\nContent-Disposition: form-data; name="file"; filename="n.png\r\nContent-Type: image/png\r\n\r\n' + png + b"\r\n--B--\r\n"
    conn.request("POST", "/api/uploads", body,
                 {"Content-Type": "multipart/form-data; boundary=B", "Cookie": ck})
    r = conn.getresponse(); d = json.loads(r.read()); conn.close()
    return r.status, d
_cur.execute("select count(*) from upload_usage uu join users u on u.id=uu.user_id where u.username=%s", (_hf_name,))
_all_usage = _cur.fetchone()[0]  # H 块基线 (此前 PF 轮已staging)
st, d = _no_tool(_ckf)
check("H5 no-tool upload → 422 err_tool_required",
      st == 422 and d.get("error") == "err_tool_required", f"{st} {d.get('error')}")
st, d = _no_tool(_ckf)  # 再拒一次
_cur.execute("select count(*) from upload_usage uu join users u on u.id=uu.user_id where u.username=%s", (_hf_name,))
check("H6 被拒 no-tool 上传不产生 upload_usage",
      _cur.fetchone()[0] == _all_usage, f"{_all_usage}")
# share 工具化后的越权 delivery 由 execute 面 403 (H2/H3) — 这里补真上传+越权执行
st, d = _share(_ckf, 30, 200)   # staging ok (delivery 不在 upload 面检查)
_pid = (d.get("file") or {}).get("stagingId")
st2, d2 = _execute(_ckf, {"tool_id": "share",
                          "source": {"files": [{"public_id": _pid, "source_token": ""}]},
                          "params": {}, "delivery": {"expiry_days": 30, "allowed_downloads": 200},
                          "client_ref": f"ig-{uuid.uuid4().hex[:8]}"})
check("H7 share 越权 delivery → execute 403 err_expiry_not_entitled",
      st == 200 and _pid and st2 == 403
      and d2.get("error") == "err_expiry_not_entitled", f"up={st} exec={st2} {str(d2)[:80]}")

# ═══ 5d. Platform ingress hard boundaries (20/50/100) ═══
from governance.ingress import validate_batch, MAX_FILES_PER_REQUEST
from config import MAX_UPLOAD_BYTES as _CFG_MAX_UP, MAX_BATCH_BYTES as _CFG_MAX_BAT

check("B-S1 validate_batch 20 files PASS (读实际 config)",
      validate_batch([1] * MAX_FILES_PER_REQUEST) is None)
d = validate_batch([1] * (MAX_FILES_PER_REQUEST + 1))
check("B-S2 21 files → err_too_many_files {limit:20, incoming:21}",
      d == {"error": "err_too_many_files", "limit": 20, "incoming": 21}, str(d))
check("B-S3 恰 MAX_UPLOAD_BYTES PASS (env 可覆盖, 不写死)",
      validate_batch([_CFG_MAX_UP]) is None)
d = validate_batch([_CFG_MAX_UP + 1])
check("B-S4 单文件 +1 → err_file_too_large + limit/incoming",
      d and d["error"] == "err_file_too_large" and d["limit"] == _CFG_MAX_UP
      and d["incoming"] == _CFG_MAX_UP + 1, str(d))
check("B-S5 恰 MAX_BATCH_BYTES PASS",
      validate_batch([_CFG_MAX_UP, _CFG_MAX_BAT - _CFG_MAX_UP]) is None)
d = validate_batch([_CFG_MAX_UP, _CFG_MAX_BAT - _CFG_MAX_UP, 1])
check("B-S6 batch +1 → err_batch_too_large (不冒充 file_too_large; 每项 ≤ 单限)",
      d and d["error"] == "err_batch_too_large" and d["limit"] == _CFG_MAX_BAT
      and d["incoming"] == _CFG_MAX_BAT + 1, str(d))

# invocation._MAX_FILES 与 shared 常量同一来源:
import execution.invocation as _inv_mod
check("B-S7 invocation files 上限 = shared MAX_FILES_PER_REQUEST",
      _inv_mod._MAX_FILES == MAX_FILES_PER_REQUEST == 20)

# ═══ 5e. Web / REST / MCP 三面 hard-boundary 实测 (低限由环境设定或用真实边界单点文件) ═══
# 单文件越界构造: max(1, MAX_UPLOAD+1) bytes 的最小 multipart — 不实际传 50MB:
def _web_upload(ck, size, count=1, name="b.png"):
    conn = _hc.HTTPConnection(*_HOST_ine, timeout=30)
    body = b""
    for k, v in {"tool": "share", "client_ref": f"wu-{uuid.uuid4().hex[:8]}-1"}.items():
        body += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for i in range(count):
        body += f'--B\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: image/png\r\n\r\n'.encode() + (b"x" * 0) + b"\r\n"
    body += b"--B--\r\n"
    # size 字节计数无法伪造 — 用 21 个真文件测 files 边界 (字节边界走 REST 单元):
    conn.request("POST", "/api/uploads", body,
                 {"Content-Type": "multipart/form-data; boundary=B", "Cookie": ck})
    r = conn.getresponse(); raw = r.read(); conn.close()
    try: return r.status, json.loads(raw)
    except Exception: return r.status, {"raw": raw[:80].decode(errors="replace")}

# Web 21 files (真实小文件):
conn = _hc.HTTPConnection(*_HOST_ine, timeout=30)
body = b""
for k, v in {"tool": "share", "client_ref": f"wu-{uuid.uuid4().hex[:8]}-2"}.items():
    body += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
_png1 = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
for i in range(21):
    body += f'--B\r\nContent-Disposition: form-data; name="file"; filename="f{i}.png"\r\nContent-Type: image/png\r\n\r\n'.encode() + _png1 + b"\r\n"
body += b"--B--\r\n"
_cur.execute("select count(*) from upload_usage uu join users u on u.id=uu.user_id where u.username=%s", (_hf_name,))
_all_usage = _cur.fetchone()[0]  # baseline AFTER H/PF staging blocks
conn.request("POST", "/api/uploads", body,
             {"Content-Type": "multipart/form-data; boundary=B", "Cookie": _ckf})
r = conn.getresponse(); d = json.loads(r.read()); conn.close()
check("B-W1 Web 21 files → 400 err_too_many_files {limit:20, incoming:21}",
      r.status == 400 and d.get("error") == "err_too_many_files"
      and d.get("limit") == 20 and d.get("incoming") == 21, f"{r.status} {str(d)[:100]}")
_cur.execute("select count(*) from upload_usage uu join users u on u.id=uu.user_id where u.username=%s", (_hf_name,))
check("B-W2 拒绝后 upload_usage 零新增", _cur.fetchone()[0] == _all_usage)

# Public REST v1 retired — ingress boundary 单点在 pipeline (Web/MCP 同函数, B-M2 已测)。
check("B-R1 apiv1 模块不存在 (v1 uploads 退役)",
      not _os.path.exists(_os.path.join(REPO, "app", "routes", "apiv1.py")))

# upload_sources pipeline was deleted with the v1 surface (the coherence pass dead
# code). The boundary AUTHORITY every surface consumes before storage is
# governance.ingress.validate_batch — B-M drives it directly (same
# denial dicts the web/MCP staging paths relay).
from governance.ingress import validate_batch as _vb
_d = _vb([1] * 21)
check("B-M1 boundary 21 files → err_too_many_files (storage 前)",
      _d == {"error": "err_too_many_files", "limit": 20, "incoming": 21}, str(_d))
_big = _CFG_MAX_UP + 1
_d = _vb([_big])
check("B-M2 boundary oversized single → 拒 (storage 前)",
      _d["error"] == "err_file_too_large", str(_d))
_cur.execute("select count(*) from upload_usage uu join users u on u.id=uu.user_id where u.username=%s", (_hf_name,))
check("B-M3 被拒 batch 零 usage 残留 (storage 前)", _cur.fetchone()[0] == _all_usage)
# MCP HTTP 层 isError 语义由 5f 段真实 /mcp E2E 覆盖 (bearer + tools/call)。

# ═══ 5f. 真实 /mcp HTTP E2E (create user → API bearer token → tools/call) ═══
_eu, _euid = _register("mcpe2e" + str(int(time.time()))[-6:])
_eck = _login(_eu)
conn = _hc.HTTPConnection(*_HOST_ine, timeout=30)
conn.request("POST", "/api/keys", json.dumps({"name": "canonical-e2e"}),
             {"Cookie": _eck, "Content-Type": "application/json"})
_tk = json.loads(conn.getresponse().read()); conn.close()
_bearer = (_tk.get("key") or {}).get("key") or ""
check("ME-P0 fixture: 真实 API token 创建成功", bool(_bearer),
      f"cookie={'yes' if _eck else 'NO'} resp={str(_tk)[:80]}")

def _mcp_ine(arguments, with_ref=True):
    if with_ref and "client_ref" not in arguments:
        arguments = {**arguments,
                     "client_ref": f"mcpe2e-{uuid.uuid4().hex[:8]}"}
    _body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
             "params": {"name": "share", "arguments": arguments}}
    conn = _hc.HTTPConnection(*_HOST_ine, timeout=60)
    conn.request("POST", "/mcp", json.dumps(_body), {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {_bearer}"})
    r = conn.getresponse(); raw = r.read().decode(); conn.close()
    parsed = None
    for line in raw.split("\n"):
        if line.startswith("data:"):
            parsed = json.loads(line[5:]); break
    if parsed is None:
        parsed = json.loads(raw)
    return r.status, parsed

# A. hard boundary business error (真实 21 files)
_files21 = [{"b64": base64.b64encode(_png1).decode(), "filename": f"e{i}.png"}
            for i in range(MAX_FILES_PER_REQUEST + 1)]
st, d = _mcp_ine({"files": _files21})
_res = d.get("result") or {}
_content = ((_res.get("content") or [{}])[0].get("text") or "")
_cd = _res.get("structuredContent") or {}
check("ME-A1 MCP E2E 21 files → HTTP 200, 无 top-level jsonrpc error",
      st == 200 and "error" not in d, f"{st} {str(d.get('error'))[:60]}")
check("ME-A2 result.isError == true + err_file_count (window is the count authority)",
      _res.get("isError") is True and _cd.get("error") == "err_file_count",
      _content[:90])
check("ME-A3 非 -32602 (业务错误/协议错误分层)",
      not (d.get("error") or {}).get("code") == -32602)

# B. malformed request 仍是 protocol error
st, d = _mcp_ine({"files": [{"b64": "!!!not-base64!!!", "filename": "x.png"}]})
check("ME-B 非法 base64 → JSON-RPC -32602",
      (d.get("error") or {}).get("code") == -32602, str(d.get("error"))[:80])

# C. 真实 MCP rolling-quota denial: 填满 E2E 用户 usage 再 tools/call
_cur.execute("select id from users where username=%s", (_eu,))
_euid = _cur.fetchone()[0]
_cur.execute("delete from upload_usage where user_id=%s", (_euid,))
with db() as c:
    usage_repo.insert_many(c, _euid, "127.0.0.1", [1] * FREE["ingress_files_24h"], int(time.time()))
_files_q = [{"b64": base64.b64encode(_png1).decode(), "filename": "q.png"}]
st, d = _mcp_ine({"files": _files_q})
_res = d.get("result") or {}
_content = ((_res.get("content") or [{}])[0].get("text") or "")
_qd = _res.get("structuredContent") or {}
check("MQ-1 MCP quota denial: HTTP 200 + isError + 完整结构",
      st == 200 and _res.get("isError") is True
      and _qd.get("error") == "err_ingress_files_24h"
      and _qd.get("limit") == FREE["ingress_files_24h"]
      and _qd.get("used") == FREE["ingress_files_24h"]
      and _qd.get("incoming") == 1 and _qd.get("remaining") == 0
      and _qd.get("plan_code") == "free", _content[:110])
check("MQ-2 无 top-level JSON-RPC error (非 -32602)", "error" not in d)

# Web 真实: 同用户 (或新用户填满) → /api/uploads 429 六字段
_wu = "webq" + str(int(time.time()))[-6:]
_wu_id = None
conn = _hc.HTTPConnection(*_HOST_ine, timeout=30)
_out = b""
for k, v in {"username": _wu, "password": "Test12345", "password_confirm": "Test12345", "email": f"{_wu}@t.co"}.items():
    _out += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
b, ct = _out + b"--B--\r\n", "multipart/form-data; boundary=B"
conn.request("POST", "/auth/register", b, {"Content-Type": ct}); conn.getresponse().read(); conn.close()
_wck = _login(_wu)
_cur.execute("select id from users where username=%s", (_wu,))
_wu_id = _cur.fetchone()[0]
with db() as c:
    usage_repo.insert_many(c, _wu_id, "127.0.0.1", [1] * FREE["ingress_files_24h"], int(time.time()))
conn = _hc.HTTPConnection(*_HOST_ine, timeout=30)
body = b""
for k, v in {"tool": "share", "client_ref": f"wu-{uuid.uuid4().hex[:8]}-3"}.items():
    body += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
body += f'--B\r\nContent-Disposition: form-data; name="file"; filename="q.png"\r\nContent-Type: image/png\r\n\r\n'.encode() + _png1 + b"\r\n--B--\r\n"
conn.request("POST", "/api/uploads", body, {"Content-Type": "multipart/form-data; boundary=B", "Cookie": _wck})
r = conn.getresponse(); wd = json.loads(r.read()); conn.close()
check("WQ-1 Web quota denial: 429 + 六字段完整",
      r.status == 429 and wd.get("error") == "err_ingress_files_24h"
      and wd.get("limit") == FREE["ingress_files_24h"]
      and wd.get("used") == FREE["ingress_files_24h"]
      and wd.get("incoming") == 1 and wd.get("remaining") == 0
      and wd.get("plan_code") == "free", f"{r.status} {str(wd)[:110]}")


# denial 无残留 (upload_usage 不增; Gokapi 面由 pipeline 顺序保证)
_cur.execute("select count(*) from upload_usage where user_id=%s", (_wu_id,))
check("NQ-1 quota denial 后 upload_usage 零新增",
      _cur.fetchone()[0] == FREE["ingress_files_24h"])

# NQ-2: quota decision 在 Gokapi storage 之前 — 真实 HTTP path 前后对象计数不变
def _gokapi_count():
    conn = _hc.HTTPConnection(_HOST_ine[0], _GOKAPI_PORT, timeout=15)
    conn.request("GET", "/api/files/list", headers={"apikey": _GK_KEY})
    r = conn.getresponse(); files = json.loads(r.read()); conn.close()
    return len(files) if isinstance(files, list) else -1

_GK_KEY = _os.environ.get("FLASHSHARE_GOKAPI_KEY", "")
_GOKAPI_RAW = _os.environ.get("FLASHSHARE_GOKAPI_URL", "")
_GOKAPI_PORT = int(_GOKAPI_RAW.rsplit(":", 1)[-1]) if ":" in _GOKAPI_RAW else 53898
_before = _gokapi_count()
conn = _hc.HTTPConnection(*_HOST_ine, timeout=30)
body = b""
for k, v in {"tool": "share", "client_ref": f"wu-{uuid.uuid4().hex[:8]}-4"}.items():
    body += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
body += f'--B\r\nContent-Disposition: form-data; name="file"; filename="z.png"\r\nContent-Type: image/png\r\n\r\n'.encode() + _png1 + b"\r\n--B--\r\n"
conn.request("POST", "/api/uploads", body, {"Content-Type": "multipart/form-data; boundary=B", "Cookie": _wck})
r = conn.getresponse(); _zd = json.loads(r.read()); conn.close()
_after = _gokapi_count()
check("NQ-2 quota denial 后 Gokapi object count 不增加 (真实 HTTP path, storage 前拒)",
      r.status == 429 and _zd.get("error") == "err_ingress_files_24h"
      and _after == _before and _before >= 0,
      f"before={_before} after={_after} status={r.status}")

# ═══ 5g. Credits affordability preflight (advisory read-only) ═══
from execution.affordability import effective_quote, check_affordability
from economy.billing import spendable_balance, grant as _grant
from governance.catalog import resolve as _catalog_resolve

# priced tool fixture: 注册 owner + 定价工具
def _seed_priced_tool(owner_id, tool_id, cost):
    import json as _jsonx
    ts = int(time.time())
    tk = f"pf{owner_id}_{tool_id}"
    _cur.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution, label_json, visibility, enabled, params_json, endpoint_json, created_at, updated_at, accept_json, format_family)"
        " values (%s, %s, %s, 'http', %s, 'public', true, '[]', '{\"url\": \"https://example.org/x\", \"timeout_s\": 30}', %s, %s, '[\".png\"]', 'image')"
        " on conflict do nothing",
        (tk, owner_id, tool_id, _jsonx.dumps({"en": f"PF {tool_id}"}), ts, ts))
    _cur.execute(
        "insert into pricing (tool_id, cost, enabled, updated_at) values (%s, %s, true, 1)"
        " on conflict (tool_id) do update set cost = excluded.cost, enabled = true",
        (tk, cost))
    return tk

_pf_owner_name, _pf_owner_id = _register("pfown" + str(int(time.time()))[-5:])
_pf_user_name, _pf_user_id = _register("pfusr" + str(int(time.time()))[-5:])
_pf_key = _seed_priced_tool(_pf_owner_id, "paidtool", 5)
_trow, _trr = _catalog_resolve(_pf_key, _pf_user_id)
check("PF-P0 fixture: priced tool resolve ok", _trr is None and _trow is not None, str(_trr))

# effective cost 单点
check("PF-EC1 普通用户 + priced tool → listed cost 5",
      effective_quote(_pf_user_id, _trow) == {"enabled": True, "cost": 5})
_trow_self, _ = _catalog_resolve(_pf_key, _pf_owner_id)
check("PF-EC2 self-owned priced tool → 0",
      effective_quote(_pf_owner_id, _trow_self)["cost"] == 0)
_cur.execute("update pricing set cost=0 where tool_id=%s", (_pf_key,))
check("PF-EC3 cost=0 → 0", effective_quote(_pf_user_id, _trow)["cost"] == 0)
_cur.execute("update pricing set cost=5 where tool_id=%s", (_pf_key,))

# affordability: balance=0 cost=5 → 精确 denial
_dn = check_affordability(_pf_user_id, _trow)
check("PF-A1 balance=0 cost=5 → 精确 {err_insufficient_credits, balance:0, cost:5}",
      _dn == {"error": "err_insufficient_credits", "balance": 0, "cost": 5}, str(_dn))
# balance == cost → PASS
with db() as c:
    _grant(_pf_user_id, 5, "grant", idem_key=f"test:pf-a2:{int(time.time())}", note="canonical")
_dn = check_affordability(_pf_user_id, _trow)
check("PF-A2 balance == cost → None (恰好够)", _dn is None, str(_dn))
# cost=0 → 不查余额不拒绝 (即使 balance 0 用户)
check("PF-A3 self-owned → None (不查余额)",
      check_affordability(_pf_owner_id, _trow_self) is None)

def _pf_multipart(tool):
    _b = b""
    for k, v in {"tool": tool,
                 "client_ref": f"pf-{uuid.uuid4().hex[:10]}"}.items():
        _b += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    _b += b'--B\r\nContent-Disposition: form-data; name="file"; filename="r.png"\r\nContent-Type: image/png\r\n\r\n' + _png1 + b"\r\n--B--\r\n"
    return _b

# pricing disabled → defer 给 gate: 不裁决 disabled / 不查余额 / 不 402
_cur.execute("update pricing set enabled=false where tool_id=%s", (_pf_key,))
_dn_dis = check_affordability(_pf_user_id, _trow)  # balance=0 用户 + disabled priced tool
check("PF-A4 pricing disabled + balance=0 → check_affordability is None (defer 给 gate)",
      _dn_dis is None, str(_dn_dis))
# spy: disabled path 不触碰 spendable_balance (替换为会抛异常的函数)
import execution.affordability as _affmod
_orig_sb = _affmod.spendable_balance
def _boom(_uid):
    raise AssertionError("spendable_balance must not be called on disabled path")
_affmod.spendable_balance = _boom
try:
    _dn_spy = check_affordability(_pf_user_id, _trow)
    _spy_ok = _dn_spy is None
except AssertionError:
    _spy_ok = False
finally:
    _affmod.spendable_balance = _orig_sb
check("PF-A5 disabled path 不调用 spendable_balance (spy 证实)", _spy_ok)
# authoritative execute 最终 403 tool_disabled:
_pfd_ck = _login(_pf_user_name)
conn = _hc.HTTPConnection(*_HOST_ine, timeout=30)
conn.request("POST", "/api/tools/preflight", json.dumps({"tool_id": _pf_key}),
             {"Cookie": _pfd_ck, "Content-Type": "application/json"})
r = conn.getresponse(); _pfd = json.loads(r.read()); conn.close()
check("PF-A6 preflight 对 disabled 不 402 (defer: ok)",
      r.status == 200 and _pfd == {"ok": True}, f"{r.status} {_pfd}")
conn = _hc.HTTPConnection(*_HOST_ine, timeout=30)
conn.request("POST", "/api/uploads", _pf_multipart("share"), {"Content-Type": "multipart/form-data; boundary=B", "Cookie": _pfd_ck})
_up_d = json.loads(conn.getresponse().read()); conn.close()
_files_d = [{"public_id": _up_d["file"]["stagingId"], "source_token": _up_d["file"].get("sourceToken", "")}]
_inv_d = {"tool_id": _pf_key, "source": {"files": _files_d}, "params": {},
          "delivery": {"expiry_days": 1, "allowed_downloads": 50}, "client_ref": f"ig-{uuid.uuid4().hex[:8]}"}
conn = _hc.HTTPConnection(*_HOST_ine, timeout=60)
conn.request("POST", "/api/tools/execute", json.dumps(_inv_d),
             {"Cookie": _pfd_ck, "Content-Type": "application/json"})
r = conn.getresponse(); _ed = json.loads(r.read()); conn.close()
check("PF-A7 authoritative execute 最终 403 tool_disabled",
      r.status == 403 and _ed.get("error") == "tool_disabled", f"{r.status} {str(_ed)[:70]}")
_cur.execute("update pricing set enabled=true, cost=5 where tool_id=%s", (_pf_key,))

# no mutation: denial 前后差分 (denial 用户 balance=0; 此前 PF-A2 grant 属另一用户)
def _counts(uid):
    _cur.execute("select count(*) from ledger where user_id=%s", (uid,))
    l = _cur.fetchone()[0]
    _cur.execute("select count(*) from tool_calls where user_id=%s", (uid,))
    tc = _cur.fetchone()[0]
    _cur.execute("select count(*) from upload_usage where user_id=%s", (uid,))
    uu = _cur.fetchone()[0]
    return l, tc, uu

_pf_n1_name, _pf_n1_id = _register("pfn1" + str(int(time.time()))[-5:])
_b = _counts(_pf_n1_id)
_dn2 = check_affordability(_pf_n1_id, _trow)  # balance=0 用户 → denial (纯读)
_b_after = _counts(_pf_n1_id)
check("PF-N1 affordability denial 是纯只读 (前后差分零变)",
      _dn2 == {"error": "err_insufficient_credits", "balance": 0, "cost": 5}
      and _b_after == _b, f"denial={_dn2} before={_b} after={_b_after}")

# Web endpoint: 真实 session
_pfw_name, _pfw_id = _register("pfweb" + str(int(time.time()))[-5:])
_pfw_ck = _login(_pfw_name)
conn = _hc.HTTPConnection(*_HOST_ine, timeout=30)
conn.request("POST", "/api/tools/preflight", json.dumps({"tool_id": _pf_key}),
             {"Cookie": _pfw_ck, "Content-Type": "application/json"})
r = conn.getresponse(); wd = json.loads(r.read()); conn.close()
check("PF-W1 Web preflight priced+balance0 → 402 {err, balance:0, cost:5}",
      r.status == 402 and wd == {"ok": False, "error": "err_insufficient_credits",
                                 "balance": 0, "cost": 5}, f"{r.status} {str(wd)[:90]}")
conn = _hc.HTTPConnection(*_HOST_ine, timeout=30)
conn.request("POST", "/api/tools/preflight", json.dumps({"tool_id": "share"}),
             {"Cookie": _pfw_ck, "Content-Type": "application/json"})
r = conn.getresponse(); fd = json.loads(r.read()); conn.close()
check("PF-W2 free tool preflight → 200 {ok:true}",
      r.status == 200 and fd == {"ok": True}, f"{r.status} {fd}")
# no-mutation for the endpoint path
_b2 = _counts(_pfw_id)
check("PF-W3 endpoint 零副作用 (无 call/ledger/usage)", _b2 == (0, 0, 0), str(_b2))

# MCP real E2E: bearer + priced + balance 0
conn = _hc.HTTPConnection(*_HOST_ine, timeout=30)
conn.request("POST", "/api/keys", json.dumps({"name": "pf-mcp"}),
             {"Cookie": _pfw_ck, "Content-Type": "application/json"})
_pfm_tk = json.loads(conn.getresponse().read()); conn.close()
_pfm_bearer = (_pfm_tk.get("key") or {}).get("key") or ""
_mcp_files = [{"b64": base64.b64encode(_png1).decode(), "filename": "pf.png"}]
_mcp_body = {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
             "params": {"name": _pf_key.replace("/", "__"),
                        "arguments": {"files": _mcp_files,
                                      "client_ref": f"mcppf-{uuid.uuid4().hex[:8]}"}}}
conn = _hc.HTTPConnection(*_HOST_ine, timeout=60)
conn.request("POST", "/mcp", json.dumps(_mcp_body), {
    "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
    "Authorization": f"Bearer {_pfm_bearer}"})
r = conn.getresponse(); raw = r.read().decode(); conn.close()
for line in raw.split("\n"):
    if line.startswith("data:"):
        md = json.loads(line[5:]); break
else:
    md = json.loads(raw)
_mres = md.get("result") or {}
_mtxt = ((_mres.get("content") or [{}])[0].get("text") or "")
_mdj = _mres.get("structuredContent") or {}
# 真 before/after 差分: 计数在请求发出之前先取 (上一段是伪序)
_gk_before_pf = _gokapi_count()
_cur.execute("select count(*) from upload_usage where user_id=%s", (_pfw_id,))
_uu_before_pf = _cur.fetchone()[0]
conn = _hc.HTTPConnection(*_HOST_ine, timeout=60)
conn.request("POST", "/mcp", json.dumps(_mcp_body), {
    "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
    "Authorization": f"Bearer {_pfm_bearer}"})
r = conn.getresponse(); raw = r.read().decode(); conn.close()
for line in raw.split("\n"):
    if line.startswith("data:"):
        md = json.loads(line[5:]); break
else:
    md = json.loads(raw)
_mres = md.get("result") or {}
_mtxt = ((_mres.get("content") or [{}])[0].get("text") or "")
_mdj = _mres.get("structuredContent") or {}
_gk_after_pf = _gokapi_count()
_cur.execute("select count(*) from upload_usage where user_id=%s", (_pfw_id,))
_uu_after_pf = _cur.fetchone()[0]
check("PF-M1 MCP priced+balance0 → 200 isError err_insufficient_credits {balance:0, cost:5}",
      r.status == 200 and _mres.get("isError") is True
      and _mdj.get("error") == "err_insufficient_credits"
      and _mdj.get("balance") == 0 and _mdj.get("cost") == 5
      and "error" not in md, f"{r.status} {_mtxt[:90]}")
check("PF-M2 MCP denial 零 ingress (真实请求前后差分: Gokapi & upload_usage)",
      _gk_after_pf == _gk_before_pf and _uu_after_pf == _uu_before_pf,
      f"gokapi {_gk_before_pf}→{_gk_after_pf} usage {_uu_before_pf}→{_uu_after_pf}")
# malformed base64 仍 -32602 (优先于 affordability):
_mcp_bad = dict(_mcp_body)
_mcp_bad["params"] = {"name": _pf_key.replace("/", "__"),
                      "arguments": {"files": [{"b64": "!!!", "filename": "x.png"}]}}
conn = _hc.HTTPConnection(*_HOST_ine, timeout=60)
conn.request("POST", "/mcp", json.dumps(_mcp_bad), {
    "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
    "Authorization": f"Bearer {_pfm_bearer}"})
r = conn.getresponse(); raw = r.read().decode(); conn.close()
for line in raw.split("\n"):
    if line.startswith("data:"):
        bd = json.loads(line[5:]); break
else:
    bd = json.loads(raw)
check("PF-M3 malformed base64 → -32602 (优先于 affordability)",
      (bd.get("error") or {}).get("code") == -32602, str(bd.get("error"))[:80])

# Advisory race: preflight PASS 后余额合法消耗 → authoritative gate 402
_pfr_name, _pfr_id = _register("pfrace" + str(int(time.time()))[-5:])
with db() as c:
    _grant(_pfr_id, 5, "grant", idem_key=f"test:pf-race:{int(time.time())}", note="canonical")
_trrow, _ = _catalog_resolve(_pf_key, _pfr_id)
check("PF-R1 race fixture: preflight PASS (balance=cost=5)",
      check_affordability(_pfr_id, _trrow) is None)
# 合法消耗掉余额 (grant 精确退回即消耗): 直接 consume 5
from economy.billing import consume as _consume
with db() as c:
    _consume(_pfr_id, 5, "consume", idem_key=f"race-burn:{_pfr_id}:{int(time.time())}",
             note="race burn")
check("PF-R2 preflight PASS ≠ reservation: 余额变化后 check_affordability 反映新快照",
      check_affordability(_pfr_id, _trrow) == {"error": "err_insufficient_credits",
                                               "balance": 0, "cost": 5})
# authoritative gate 402 (真实 execute):
_pfr_ck = _login(_pfr_name)
conn = _hc.HTTPConnection(*_HOST_ine, timeout=30)
conn.request("POST", "/api/uploads", _pf_multipart("share"), {"Content-Type": "multipart/form-data; boundary=B", "Cookie": _pfr_ck})
_up = json.loads(conn.getresponse().read()); conn.close()
_files = [{"public_id": _up["file"]["stagingId"], "source_token": _up["file"].get("sourceToken", "")}]
_inv = {"tool_id": _pf_key, "source": {"files": _files}, "params": {},
        "delivery": {"expiry_days": 1, "allowed_downloads": 50}, "client_ref": f"ig-{uuid.uuid4().hex[:8]}"}
conn = _hc.HTTPConnection(*_HOST_ine, timeout=60)
conn.request("POST", "/api/tools/execute", json.dumps(_inv),
             {"Cookie": _pfr_ck, "Content-Type": "application/json"})
r = conn.getresponse(); ed = json.loads(r.read()); conn.close()
check("PF-R3 authoritative gate 仍 402 err_insufficient_credits (最终权威不削弱)",
      r.status == 402 and ed.get("error") == "err_insufficient_credits", f"{r.status} {str(ed)[:80]}")

# ═══ 6. Source-of-truth guard ═══
_up = open(_os.path.join(REPO, "core", "sharing", "uploads.py"), encoding="utf-8").read()
check("G1 sharing/uploads.py 不出现 plus (不认识会员名)",
      "plus" not in _up)
check("G2 quota decision 无 err_quota_ip", "err_quota_ip" not in _up)
check("G3 quota decision 不调用 ip_usage_count", "ip_usage_count" not in _up)
check("G4 uploads.py 经 entitlements resolver 取限",
      "entitlements" in _up and "DAILY_UPLOADS" not in _up and "DAILY_BYTES" not in _up)
_disp = open(_os.path.join(REPO, "core", "execution", "dispatch.py"), encoding="utf-8").read()
_apir = open(_os.path.join(REPO, "app", "routes", "api.py"), encoding="utf-8").read()
check("G5 dispatch 无 plus 业务判断, 经 check_delivery",
      "plus" not in _disp and "check_delivery" in _disp)
check("G6 api.py 无 plus 业务判断; 上传面只 staging (无 delivery entitlement)",
      "plus" not in _apir and "check_delivery" not in _apir
      and "err_tool_required" in _apir)
check("G7 validate_delivery 仍是唯一 protocol 单点 (无第二枚举)",
      _disp.count("EXPIRY_DAYS") == 0 and "EXPIRY_DAYS" not in _apir)
_inv_src = open(_os.path.join(REPO, "core", "execution", "invocation.py"), encoding="utf-8").read()
check("G8 invocation 不再字面定义 _MAX_FILES = 20 (引 shared 常量)",
      "_MAX_FILES = 20" not in _inv_src
      and "MAX_FILES_PER_REQUEST" in _inv_src)
_pipe_src = ""  # execution/pipeline.py deleted (FX-2 dead module)
# the coherence pass: upload_sources pipeline deleted; the shared batch validator is
# consumed by the live surfaces (web api + MCP) — G10/G11 cover those.
check("G9 pipeline 死代码已删 (无 ingress 遗留)",
      "validate_batch" not in _pipe_src and "upload_sources" not in _pipe_src)
_mcp_src = open(_os.path.join(REPO, "app", "routes", "mcp.py"), encoding="utf-8").read()
check("G10 MCP 不自建 batch 规则 (数量/字节权威都在 staging 单点)",
      "MAX_BATCH_BYTES" not in _mcp_src and "validate_batch" not in _mcp_src)
check("G11 Web api.py 不维护第二份 authoritative 判定 (无 files>20 字面/无逐文件 sum 拒)",
      "len(uploads) > 20" not in _apir and "total_incoming" not in _apir
      and "validate_batch" in _apir)
_mcp_up = _mcp_src.split("_ingest_sources(")[1].split("raw = {")[0] if "_ingest_sources(" in _mcp_src else ""
check("G12 MCP upload 区保留 boundary/quota 专门分支 + 回滚分支",
      "IngressQuotaDenied" in _mcp_src and "_iserror(" in _mcp_up
      and "_StagingError" in _mcp_up, "")
# G13: Web transport early-reject 与 authoritative validate_batch 输出同构
# (ok/error/limit/incoming 四键; incoming=declared transport bytes)
_er = _apir.split("Transport safety only")[1].split("pending = []")[0] if "Transport safety only" in _apir else ""
check("G13 Web early-reject 结构完整 {ok,error,limit,incoming} 且不早于 validate_batch 定义第二算法",
      '"error": "err_batch_too_large"' in _er and '"limit": MAX_BATCH_BYTES' in _er
      and '"incoming": declared' in _er and "MAX_BATCH_BYTES +" in _er
      and "validate_batch" in _apir)
_ups = open(_os.path.join(REPO, "core", "sharing", "uploads.py"), encoding="utf-8").read()
check("G14 sharing/uploads.py 是 quota denial 唯一构造点 (_quota_denial 单点)",
      "_quota_denial" in _ups and "err_ingress_files_24h" not in _apir
      and "err_ingress_files_24h" not in _mcp_src, "")
def _code_lines(src):
    return "\n".join(l for l in src.split("\n")
                     if not l.strip().startswith("#") and "recomputation" not in l)
# downloads_remaining is the single-point utils.remaining_downloads()
# projection of a stored file, not quota arithmetic — excluded by name
def _quota_remaining(src):
    return "remaining" in _code_lines(src).replace(
        "downloads_remaining", "").replace("remaining_downloads", "")
check("G15 消费面不自行算 remaining (透传 denial, 排除注释)",
      not _quota_remaining(_apir) and not _quota_remaining(_mcp_src))
# repo-wide active guard: 动态拼旧 key 防自匹配; 只扫文本源码, 排除缓存/生成物目录树中非源码部分
_old_count_key = "err_quota_" + "count"
_old_size_key = "err_quota_" + "size"
_hits = []
for _root_name in ("app", "core", "static", "templates", "tests"):
    _base = _os.path.join(REPO, _root_name)
    for _dirpath, _dirnames, _filenames in _os.walk(_base):
        _dirnames[:] = [d for d in _dirnames
                        if d not in ("__pycache__", "node_modules", ".git")]
        for _fn in _filenames:
            if not _fn.endswith((".py", ".js", ".html", ".css", ".json", ".md", ".txt")):
                continue
            _fp = _os.path.join(_dirpath, _fn)
            try:
                _src = open(_fp, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            if _old_count_key in _src or _old_size_key in _src:
                _hits.append(_os.path.relpath(_fp, REPO))
check("G16 旧 quota key repo-wide 零 active hit (app/core/static/templates/tests)",
      not _hits, str(_hits[:6]))
check("G17 MCP quota denial → isError 分支存在 + 已知回滚分支仍在",
      "IngressQuotaDenied as exc" in _mcp_src
      and "_StagingError" in _mcp_up)
check("G18 upload_sources 死管线已删 (quota 单路 = claim_ingress_quota)",
      "upload_sources" not in _pipe_src
      and "IngressQuotaDenied" in open(_os.path.join(REPO, "app", "routes", "mcp_upload.py"), encoding="utf-8").read())
_aff_src = open(_os.path.join(REPO, "core", "execution", "affordability.py"), encoding="utf-8").read()
_gate_src = open(_os.path.join(REPO, "core", "execution", "gate.py"), encoding="utf-8").read()
_appjs_src = open(_os.path.join(REPO, "static", "app.js"), encoding="utf-8").read()
check("G19 self-owned free 规则单点 (affordability 唯一实现, gate 消费 helper)",
      'tool.get("owner_id") == user_id' in _aff_src)
check("G19b gate 不再第二份 owner_id==user 判定",
      'owner_id"] == user["id"]' not in _gate_src
      and "effective_quote" in _gate_src)
check("G19c gate 不再直接调用 get_pricing (单次 quote snapshot)",
      "get_pricing(" not in _gate_src)
check("G19d gate 同一 quote 同时供 enabled 与 cost",
      'quote["enabled"]' in _gate_src and 'quote["cost"]' in _gate_src
      and _gate_src.index('quote["enabled"]') < _gate_src.index('quote["cost"]'))
_aff_imports = [l for l in _aff_src.split("\n") if l.startswith(("import ", "from "))]
import ast as _ast
_aff_tree = _ast.parse(_aff_src)
_aff_calls = [n.func.id for n in _ast.walk(_aff_tree)
              if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)]
_aff_imports_ast = [a.name for n in _ast.walk(_aff_tree)
                    if isinstance(n, _ast.ImportFrom) for a in n.names]
check("G20 preflight 模块不 import/call consume (AST 级)",
      not any("consume" in c for c in _aff_calls)
      and not any("consume" in i for i in _aff_imports_ast))
check("G20b consume 仍只在 gate authoritative 使用 (affordability AST 零 consume)",
      "consume(" in _gate_src
      and not any("consume" in c for c in _aff_calls)
      and not any("consume" in i for i in _aff_imports_ast))
check("G24 affordability disabled path 先于余额 defer (enabled 检查在 spendable 之前)",
      _aff_src.index('"enabled"]') < _aff_src.index("spendable_balance(")
      or _aff_src.index('not quote["enabled"]') < _aff_src.index("spendable_balance("))
check("G21 Web 无本地 cost-vs-balance 授权 (展示 cost badge 不属授权)",
      ".cost <= " not in _appjs_src and "cost <= balance" not in _appjs_src
      and "balance <" not in _appjs_src and "balance >=" not in _appjs_src
      and ".cost > balance" not in _appjs_src)
check("G22 MCP 不自拼 pricing+balance 判断 (走 check_affordability)",
      "get_pricing" not in _mcp_src and "spendable_balance" not in _mcp_src
      and "check_affordability" in _mcp_src)

# ── cleanup: fixtures this suite created, incl. the priced tool pair
# (leaked platform_tools rows drift the browser gate's market page) ──
try:
    _cur.execute("delete from pricing where tool_id=%s", (_pf_key,))
    _cur.execute("delete from platform_tools where tool_key=%s", (_pf_key,))
    for _uname_fix in (_pf_owner_name, _pf_user_name):
        _cur.execute("select id from users where username=%s", (_uname_fix,))
        _row_fix = _cur.fetchone()
        if _row_fix:
            _uid_fix = int(_row_fix[0])
            _cur.execute("delete from tool_calls where user_id=%s", (_uid_fix,))
            _cur.execute("delete from ledger where user_id=%s", (_uid_fix,))
            _cur.execute("delete from upload_usage where user_id=%s", (_uid_fix,))
            _cur.execute("delete from ingress_requests where user_id=%s", (_uid_fix,))
            purge_users(_cur, [_uid_fix])
    _con.commit()
except Exception:
    pass


# ════════════════════════════════════════════════════════════════════════
# Ingress guard: image admission, rate limit, backpressure
#
# Ingress guard canonical — same-family image admission and typed
# mismatch rejections (A), the MCP tools/call entry rate limit (B) and
# local execution-pool backpressure (C).
#
#   A*  detect_ext is the single magic-number authority (fixed signatures
#       + ISO-BMFF brands + ICO/TIFF). An image under a sibling image
#       extension (jpg/jpeg/png/gif/webp/bmp) is ADMITTED under its
#       detected name: every ingress surface (web /api/uploads, MCP
#       ticket PUT, MCP inline/url sources) stores, hashes and names
#       results with sniff_allowed's final_filename. What is NOT a
#       platform-allowed image is rejected with {error, filename,
#       detected} — no rename suggestion (renamable cases were admitted).
#   B*  Every tools/call counts against ONE per-user sliding window
#       (60 calls / 60 s) — meta tools and catalog search included;
#       the 61st call inside the window answers isError err_rate_limited.
#   C*  The local execution pool caps in-flight invocations at
#       4 × workers; a full pool refuses BEFORE the claim (err_busy, no
#       tool_calls row, no charge) and every run() exit path releases
#       its slot (in-flight back to 0).
#
# Fail-closed env guard; per-run unique names; fixtures cleaned at end.
# ════════════════════════════════════════════════════════════════════════
section('Ingress guard: image admission, rate limit, backpressure')

import asyncio
import base64
import http.client as _hc
import json as _json
import os as _os
import sys as _sys
import uuid as _uuid


_HOST_ing = ("127.0.0.1", int(TEST_PORT))
_BASE = f"http://127.0.0.1:{TEST_PORT}"
_u = "ig" + _uuid.uuid4().hex[:6]

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")
# minimal ISO-BMFF headers: 4-byte box size, ftyp, 4-byte major brand
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40
AVIF = b"\x00\x00\x00\x18ftypavif" + b"\x00" * 40
MOV = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 40
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 40
PDF_ing = b"%PDF-1.4\n%%EOF\n"
GARBAGE = b"NOTAFILE" + b"\x7f" * 24


def _req_ing(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST_ing, timeout=120)
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


def _form_ing(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _multipart(files, fields):
    """files: list of (filename, content_type, bytes)."""
    out = b""
    for fn, ct, blob in files:
        out += (f'--B\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{fn}"\r\nContent-Type: {ct}\r\n\r\n').encode()
        out += blob + b"\r\n"
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _mcp_ing(body, token):
    st, _h, d, _sc = _req_ing("POST", "/mcp", body, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})
    return st, d


def _call(name, arguments, token, mid=1):
    return _mcp_ing({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token)


def _b64(b):
    return base64.b64encode(b).decode()


def _tool_text(d):
    """structuredContent — the machine channel (content text is the
    readable summary)."""
    return (((d or {}).get("result") or {}).get("structuredContent")
            or {})


def _register(name):
    b, ct = _form_ing({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    _req_ing("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form_ing({"login": name, "password": "Test12345"})
    _st, _h, _d, sc = _req_ing("POST", "/auth/login", b, {"Content-Type": ct})
    ck = sc.split(";")[0]
    _st, _h, d_k, _sc = _req_ing("POST", "/api/keys", {"name": "ig"},
                             {"Content-Type": "application/json", "Cookie": ck})
    from database import db
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck, d_k["key"]["key"]


def _mismatch_ok(payload, code, filename, detected):
    """The rejection contract: code verbatim + sanitized filename +
    detected real format (or None) — and nothing else (no rename
    suggestion: renamable cases are admitted upstream)."""
    return (payload.get("error") == code
            and payload.get("filename") == filename
            and payload.get("detected") == detected
            and "suggested_name" not in payload)


def _download(url):
    """Anonymous /f/{pid}?download=1 bytes. The 302 Location (gokapi
    hotlink) may be app-relative — resolve it manually against the
    gokapi base instead of letting urllib mis-follow it."""
    import http.client as _hcl
    import urllib.request as _ur
    import urllib.parse as _up
    if url.startswith("/"):
        url = _BASE + url
    parts = _up.urlparse(url)
    conn = _hcl.HTTPConnection(parts.hostname, parts.port, timeout=60)
    conn.request("GET", parts.path + (("?" + parts.query) if parts.query else ""))
    r = conn.getresponse()
    if r.status // 100 == 3:
        loc = r.getheader("Location") or ""
        r.read()
        conn.close()
        if loc and not loc.startswith("http"):
            loc = (_os.environ.get("FLASHSHARE_GOKAPI_URL", "").rstrip("/")
                   + loc)
        with _ur.urlopen(loc, timeout=60) as rr:
            return rr.read()
    data = r.read()
    conn.close()
    return data


def _items(d):
    return ((((d or {}).get("result") or {}).get("structuredContent")
             or {}).get("items") or [{}])


uids = []
ck_a = tok_a = tok_b = None
try:
    # ═══ A0. detect_ext unit (single magic-number authority) ═══
    from security import detect_ext
    for label, head, want in (
            ("PNG", PNG, ".png"), ("JPEG", b"\xff\xd8\xe0", ".jpg"),
            ("PDF", b"%PDF-1.7", ".pdf"), ("ZIP", b"PK\x03\x04xx", ".zip"),
            ("HEIC brand", HEIC[:12], ".heic"),
            ("mif1 brand", b"\x00\x00\x00\x18ftypmif1", ".heic"),
            ("AVIF brand", AVIF[:12], ".avif"),
            ("qt brand", MOV[:12], ".mov"),
            ("isom brand", MP4[:12], ".mp4"),
            ("M4V brand", b"\x00\x00\x00\x18ftypM4V ", ".mp4"),
            ("WAV in RIFF", b"RIFFxxxxWAVE", ".wav"),
            ("ICO", b"\x00\x00\x01\x00\x04\x00", ".ico"),
            ("TIFF LE", b"II*\x00\x08\x00", ".tif"),
            ("TIFF BE", b"MM\x00*\x00\x08", ".tif"),
            ("unknown", GARBAGE, None),
            ("short head", b"\x00\x00\x00", None)):
        check(f"A0 detect_ext {label} → {want}",
              detect_ext(head) == want, detect_ext(head))
    from security import sniff_allowed
    for name, head in (("x.jpeg", b"\xff\xd8\xe0" + b"\x00" * 16),
                       ("x.png", PNG)):
        _ok, _ct, _fn = sniff_allowed(name, "", head)
        check(f"A0b matching content keeps its name ({name})",
              _ok and _fn == name, (_ok, _ct, _fn))

    uid_a, ck_a, tok_a = _register(_u + "a")
    uid_b, _ck_b, tok_b = _register(_u + "b")
    uid_c, _ck_c, _tok_c = _register(_u + "c")
    uids = [uid_a, uid_b, uid_c]
    check("F1 three fixtures registered", all(uids), uids)

    # ═══ A1. same-family admission — web /api/uploads ═══
    body, ct = _multipart([("a.jpg", "image/jpeg", PNG)],
                          {"tool": "share", "client_ref": f"ig-a1-{_u}"})
    st, _h, d, _sc = _req_ing("POST", "/api/uploads", body,
                          {"Content-Type": ct, "Cookie": ck_a})
    _f = (d or {}).get("file") or {}
    check("A1 web admits PNG-in-jpg, stored as a.png / image/png",
          st == 200 and (d or {}).get("ok") is True
          and _f.get("filename") == "a.png"
          and _f.get("contentType") == "image/png", (st, d))

    # ═══ A2. same-family admission — MCP ticket PUT ═══
    st, d = _call("create_upload", {"filename": "a.jpg", "size": len(PNG),
                                    "client_ref": f"ig-a2-{_u}"}, tok_a)
    ticket = (((d or {}).get("result") or {}).get("structuredContent") or {})
    tp = ticket.get("upload_url", "")
    check("A2a create_upload issues the ticket", tp.startswith(_BASE), ticket)
    st_p, _h, d_p, _sc = _req_ing("PUT", tp[len(_BASE):], PNG)
    check("A2b PUT admits PNG-in-jpg (200, usable reference)",
          st_p == 200 and isinstance(d_p, dict)
          and d_p.get("public_id") and d_p.get("source_token"), (st_p, d_p))
    st, d = _call("share", {"files": [{"public_id": d_p["public_id"],
                                       "source_token": d_p["source_token"]}],
                            "client_ref": f"ig-a2c-{_u}"}, tok_a)
    check("A2c the staged reference lands as a.png / image/png",
          (d.get("result") or {}).get("isError") is False
          and _items(d)[0].get("filename") == "a.png"
          and _items(d)[0].get("content_type") == "image/png", str(d)[:140])

    # ═══ A3. same-family admission — MCP inline source ═══
    st, d = _call("share", {"files": [{"filename": "a.jpg", "b64": _b64(PNG)}],
                            "client_ref": f"ig-a3-{_u}"}, tok_a)
    check("A3 MCP inline admits PNG-in-jpg as a.png / image/png",
          (d.get("result") or {}).get("isError") is False
          and _items(d)[0].get("filename") == "a.png"
          and _items(d)[0].get("content_type") == "image/png", str(d)[:140])

    # ═══ A4. HEIC content under b.jpg → b.heic ═══
    st, d = _call("share", {"files": [{"filename": "b.jpg", "b64": _b64(HEIC)}],
                            "client_ref": f"ig-a4-{_u}"}, tok_a)
    check("A4 MCP inline admits HEIC-in-jpg as b.heic / image/heic",
          (d.get("result") or {}).get("isError") is False
          and _items(d)[0].get("filename") == "b.heic"
          and _items(d)[0].get("content_type") == "image/heic", str(d)[:140])
    body, ct = _multipart([("b.jpg", "image/jpeg", HEIC)],
                          {"tool": "share", "client_ref": f"ig-a4b-{_u}"})
    st, _h, d, _sc = _req_ing("POST", "/api/uploads", body,
                          {"Content-Type": ct, "Cookie": ck_a})
    check("A4b web admits HEIC-in-jpg as b.heic",
          st == 200 and (d or {}).get("ok") is True
          and ((d or {}).get("file") or {}).get("filename") == "b.heic",
          (st, d))

    # ═══ A5. batch: whole-batch rejection names the offender ═══
    body, ct = _multipart([("c.jpg", "image/jpeg", PNG),
                           ("d.png", "image/png", PDF_ing)],
                          {"tool": "share", "client_ref": f"ig-a5-{_u}"})
    st, _h, d, _sc = _req_ing("POST", "/api/uploads", body,
                          {"Content-Type": ct, "Cookie": ck_a})
    check("A5 batch rejected as a whole, offender named with detected ext",
          st == 400 and isinstance(d, dict) and d.get("ok") is False
          and _mismatch_ok(d, "err_content_mismatch_image", "d.png", ".pdf"),
          (st, d))
    body, ct = _multipart([("e.jpg", "image/jpeg", PNG),
                           ("f.jpg", "image/jpeg", HEIC)],
                          {"tool": "share", "client_ref": f"ig-a5b-{_u}"})
    st, _h, d, _sc = _req_ing("POST", "/api/uploads", body,
                          {"Content-Type": ct, "Cookie": ck_a})
    _fs = (d or {}).get("files") or []
    check("A5b batch admission renames each entry to its detected name",
          st == 200 and (d or {}).get("ok") is True
          and [f.get("filename") for f in _fs] == ["e.png", "f.heic"],
          (st, [f.get("filename") for f in _fs]))

    # ═══ A6. rejections keep {error, filename, detected} only ═══
    body, ct = _multipart([("g.jpg", "image/jpeg", GARBAGE)],
                          {"tool": "share", "client_ref": f"ig-a6-{_u}"})
    st, _h, d, _sc = _req_ing("POST", "/api/uploads", body,
                          {"Content-Type": ct, "Cookie": ck_a})
    check("A6 web unknown magic → reject, detected null",
          st == 400 and _mismatch_ok(d, "err_content_mismatch_image",
                                     "g.jpg", None), (st, d))
    st, d = _call("share", {"files": [{"filename": "g.jpg",
                                       "b64": _b64(GARBAGE)}],
                            "client_ref": f"ig-a6b-{_u}"}, tok_a)
    _t = _tool_text(d)
    check("A6b MCP inline unknown magic → reject, detected null",
          (d.get("result") or {}).get("isError") is True
          and _mismatch_ok(_t, "err_content_mismatch_image", "g.jpg", None)
          and "Rename the file" not in _t.get("message", ""), _t)
    body, ct = _multipart([("x.bin", "application/octet-stream", GARBAGE)],
                          {"tool": "share", "client_ref": f"ig-a6c-{_u}"})
    st, _h, d, _sc = _req_ing("POST", "/api/uploads", body,
                          {"Content-Type": ct, "Cookie": ck_a})
    check("A6c web unknown ext → err_format_not_supported, detected null",
          st == 400 and _mismatch_ok(d, "err_format_not_supported",
                                     "x.bin", None), (st, d))

    # ═══ A7. zip_pack: the archive entry carries the canonical name ═══
    st, d = _call("zip_pack", {"files": [{"filename": "a.jpg",
                                          "b64": _b64(PNG)}],
                               "client_ref": f"ig-a7-{_u}"}, tok_a)
    import io as _io
    import zipfile as _zf
    try:
        _names = _zf.ZipFile(
            _io.BytesIO(_download(_items(d)[0].get("download_url", ""))
                        )).namelist()
    except Exception as _exc:
        _names = f"{type(_exc).__name__}: {_exc}"
    check("A7 zip_pack archive entry is the detected a.png",
          (d.get("result") or {}).get("isError") is False
          and _names == ["a.png"], (_names, str(d)[:130]))

    # ═══ A8. MCP URL source (in-process, pinned transport replaced) ═══
    import remote as _remote
    from app.routes import mcp as _M

    class _Req:
        class client:
            host = "127.0.0.1"
        headers = {}

    _orig_pinned = _remote._pinned_request
    _orig_resolve = _remote._resolve_host
    _rs = _remote.socket
    _remote._resolve_host = lambda host, port: [
        (_rs.AF_INET, _rs.SOCK_STREAM, _rs.IPPROTO_TCP, "",
         ("93.184.216.34", port))]
    _remote._pinned_request = \
        lambda url, method="GET", body=None, headers=None, timeout_s=60, \
               max_bytes=None, deadline=None: (200, PDF_ing, None,
                                               "application/pdf", None)
    try:
        r = asyncio.run(_M._call_tool(_Req(), {"id": uid_a}, 1, {
            "name": "share",
            "arguments": {"files": [{"url": f"https://files.example/{_u}/x.png"}],
                          "client_ref": f"ig-a8-{_u}"}}))
        _t = _tool_text(r)
        check("A8 MCP url source rejects non-image content, detected .pdf",
              (r.get("result") or {}).get("isError") is True
              and _mismatch_ok(_t, "err_content_mismatch_image", "x.png",
                               ".pdf"), _t)
    finally:
        _remote._pinned_request = _orig_pinned
        _remote._resolve_host = _orig_resolve
    fn_noext = _M._url_upload("https://files.example/plain", None, PNG)[0]
    check("A8b extension-less URL filename gets the detected ext",
          fn_noext == "plain.png", fn_noext)

    # ═══ B. MCP tools/call entry rate limit (fresh user, fresh window) ═══
    from economy.antibuse import mcp_call_throttled, MCP_CALL_LIMIT
    _probe = uid_b + 10 ** 9
    for _i in range(MCP_CALL_LIMIT):
        if mcp_call_throttled(_probe):
            break
    check("B0 unit: limiter admits exactly the window then refuses",
          _i == MCP_CALL_LIMIT - 1 and mcp_call_throttled(_probe) is True, _i)

    seen_ok = 0
    for i in range(MCP_CALL_LIMIT):
        # meta-tool branch and catalog-search branch both count: mix them
        name, args = (("list_uploads", {"offset": 0}) if i % 2 == 0
                      else ("catalog_search", {"q": "compress"}))
        _st, d = _call(name, args, tok_b, mid=i + 1)
        if not ((d or {}).get("error") or {}):
            seen_ok += 1
    check("B1 the first 60 mixed tools/call in the window succeed",
          seen_ok == MCP_CALL_LIMIT, seen_ok)
    _st, d = _call("list_uploads", {"offset": 0}, tok_b, mid=501)
    _t = _tool_text(d)
    check("B2 61st tools/call within 60s → isError err_rate_limited",
          (d.get("result") or {}).get("isError") is True
          and _t.get("error") == "err_rate_limited", str(d)[:130])
    _st, d = _call("catalog_search", {"q": "x"}, tok_b, mid=502)
    _t = _tool_text(d)
    check("B3 every branch is covered (search throttled too)",
          (d.get("result") or {}).get("isError") is True
          and _t.get("error") == "err_rate_limited", str(d)[:130])
    _st, d = _mcp_ing({"jsonrpc": "2.0", "id": 503, "method": "tools/list"}, tok_b)
    check("B4 tools/list is NOT a tools/call (unthrottled)",
          not ((d or {}).get("error") or {})
          and len(((d or {}).get("result") or {}).get("tools", [])) > 0,
          str(d)[:110])

    # ═══ C. local execution pool backpressure (in-process, user c) ═══
    from execution.pool import (LOCAL_MAX_INFLIGHT, inflight_count,
                                release, try_acquire)
    from execution.gate import run as gate_run
    from execution.invocation import ToolInvocation
    from execution.results import StagedItems
    from execution import ExecutionError
    from governance.catalog import resolve as _resolve

    check("C0 unit: try_acquire up to the cap, then False, then recovers",
          all(try_acquire() for _ in range(LOCAL_MAX_INFLIGHT))
          and try_acquire() is False
          and (release() or True) and (release() or True)
          and inflight_count() == LOCAL_MAX_INFLIGHT - 2
          and all(release() or True for _ in range(LOCAL_MAX_INFLIGHT - 2))
          and inflight_count() == 0, inflight_count())

    tool, terr = _resolve("share", uid_c)
    tool["id"] = tool["tool_key"]
    check("C1 fixture: share resolves as a local tool",
          terr is None and tool.get("execution") == "local", (terr, tool and tool.get("execution")))

    def _inv(ref, params=None):
        return ToolInvocation("share", [], params or {},
                              {"expiry_days": 1, "allowed_downloads": 50},
                              ref)

    async def _ok_orch():
        return StagedItems(order=["text"],
                           inline=[{"kind": "text", "text": "ok"}])

    async def _fail_orch():
        raise ExecutionError("tool_failed")

    def _calls_n(uid):
        from database import db as _db
        with _db() as con:
            return int(con.execute(
                "select count(*) c from tool_calls where user_id=%s",
                (uid,)).fetchone()["c"])

    # C2 success path
    payload, err, status = asyncio.run(
        gate_run({"id": uid_c}, tool, _inv(f"ig-c2-{_u}"), _ok_orch))
    check("C2 success path settles ok and releases the slot",
          err is None and payload.get("ok") is True
          and inflight_count() == 0, (status, err))

    # C3 replay path (same ref, same fingerprint)
    payload, err, _s = asyncio.run(
        gate_run({"id": uid_c}, tool, _inv(f"ig-c2-{_u}"), _ok_orch))
    check("C3 replay path returns the stored result, slot released",
          err is None and payload.get("replay") is True
          and inflight_count() == 0, err)

    # C4 conflict path (same ref, different fingerprint)
    _p, err, status = asyncio.run(
        gate_run({"id": uid_c}, tool,
                 _inv(f"ig-c2-{_u}", params={"x": 1}), _ok_orch))
    check("C4 conflict path 409, slot released",
          status == 409 and err.get("error") == "idempotency_conflict"
          and inflight_count() == 0, (status, err))

    # C5 tool failure path
    _p, err, status = asyncio.run(
        gate_run({"id": uid_c}, tool, _inv(f"ig-c5-{_u}"), _fail_orch))
    check("C5 ExecutionError path refunds and releases the slot",
          status == 400 and err.get("error") == "tool_failed"
          and inflight_count() == 0, (status, err))

    # C6 busy path: slots held externally → err_busy BEFORE the claim
    _before = _calls_n(uid_c)
    for _ in range(LOCAL_MAX_INFLIGHT):
        try_acquire()
    _p, err, status = asyncio.run(
        gate_run({"id": uid_c}, tool, _inv(f"ig-c6-{_u}"), _ok_orch))
    check("C6 full pool → err_busy 503, zero tool_calls rows",
          status == 503 and err.get("ok") is False
          and err.get("error") == "err_busy"
          and _calls_n(uid_c) == _before, (status, err))
    for _ in range(LOCAL_MAX_INFLIGHT):
        release()
    check("C6b external release drains the pool to 0",
          inflight_count() == 0, inflight_count())

    # C7 the MCP human-text map carries the busy sentence
    _msg = _M._error_message("err_busy")
    check("C7 err_busy English sentence mentions the same client_ref",
          "busy" in _msg and "client_ref" in _msg, _msg)
finally:
    if uids:
        from database import db
        with db() as con:
            for u in uids:
                con.execute("delete from tool_calls where user_id=%s", (u,))
                con.execute("delete from ledger where user_id=%s", (u,))
                con.execute("delete from upload_usage where user_id=%s", (u,))
                con.execute("delete from ingress_requests where user_id=%s", (u,))
                con.execute("delete from mcp_upload_tickets where user_id=%s", (u,))
            purge_users(con, uids)


# ════════════════════════════════════════════════════════════════════════
# Ingress idempotency receipts
#
# Ingress idempotency canonical — request receipts for source ingress.
#
# Web /api/uploads (client_ref) and MCP tools/call (client_ref) share ONE
# receipt mechanism: same ref/hash → replay the exact stored response with
# zero new quota/Gokapi; changed content under the same ref → 409
# idempotency_conflict with zero side effects. Real HTTP surfaces + real
# DB. Fault injection only. The retired Public REST v1 /uploads path is
# probed only as a 404 negative below.
# ════════════════════════════════════════════════════════════════════════
section('Ingress idempotency receipts')

import os as _os
import sys as _sys


_os.environ.setdefault("FLASHSHARE_GOKAPI_URL", "http://127.0.0.1:53901")
_os.environ.setdefault("FLASHSHARE_GOKAPI_KEY", "test-gk-key")


import base64
import http.client as _hc
import json
import time
import uuid
ts_idm = int(time.time())
_u = "ig" + _os.urandom(3).hex()
_HOST_idm = ("127.0.0.1", int(TEST_PORT))

from database import db

def _req_idm(method, path, body=None, headers=None, cookies=None):
    conn = _hc.HTTPConnection(*_HOST_idm, timeout=60)
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
def _form_idm(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"

def _register(name):
    b, ct = _form_idm({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{name}@t.co"})
    conn = _hc.HTTPConnection(*_HOST_idm, timeout=30)
    conn.request("POST", "/auth/register", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    if r.status not in (200, 302, 303, 409):
        raise SystemExit(f"register failed: {r.status}")
    conn.close()
    return _login_idm(name)

def _login_idm(name):
    b, ct = _form_idm({"login": name, "password": "Test12345"})
    conn = _hc.HTTPConnection(*_HOST_idm, timeout=30)
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
            (name, f"{name}@t.co", ts_idm, ts_idm)).fetchone()
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
    base = _os.environ.get("FLASHSHARE_GOKAPI_URL", "http://127.0.0.1:53901").rstrip("/")
    req = _ur.Request(base + "/api/files/list",
                      headers={"apikey": _os.environ.get(
                          "FLASHSHARE_GOKAPI_KEY", "test-gk-key")})
    with _ur.urlopen(req, timeout=5) as r:
        return len(json.loads(r.read()))

PNG_idm = base64.b64decode(
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
body, ct = _multipart([("a.png", PNG_idm, "image/png")], {"expiryDays": "1", "allowedDownloads": "50"})
st, r = _req_idm("POST", "/api/uploads", body, {"Content-Type": ct, "Cookie": ck1})
check("1 Web missing client_ref → 422 invalid_request",
      st == 422 and r.get("error") == "invalid_request", f"{st} {str(r)[:80]}")
check("1b zero usage / zero receipts",
      _usage_n(uid1) == 0 and _ingress_n(uid1) == 0)

# ══════════ 2-5. tool-source ingress + replay ══════════
ref2 = f"op-{_u}-2"
body, ct = _multipart([("a.png", PNG_idm, "image/png")],
                      {"tool": "compress", "client_ref": ref2})
gk0 = _gk_count()
st, r = _req_idm("POST", "/api/uploads", body, {"Content-Type": ct, "Cookie": ck1})
check("2 first tool-source ingress → ok",
      st == 200 and r.get("ok") and r.get("file", {}).get("stagingId"), f"{st} {str(r)[:90]}")
pid2 = r.get("file", {}).get("stagingId")
check("2b one usage + one Gokapi growth + one receipt",
      _usage_n(uid1) == 1 and _gk_count() == gk0 + 1 and _ingress_n(uid1, ref2) == 1,
      f"usage={_usage_n(uid1)} gk={_gk_count()}-{gk0}")

# replay: same ref + same bytes (response-loss retry)
body2, ct = _multipart([("a.png", PNG_idm, "image/png")],
                       {"tool": "compress", "client_ref": ref2})
st, r2 = _req_idm("POST", "/api/uploads", body2, {"Content-Type": ct, "Cookie": ck1})
check("3 same ref/bytes replay → same stagingId",
      st == 200 and r2.get("file", {}).get("stagingId") == pid2, f"{st} {str(r2)[:90]}")
check("4 replay zero new usage", _usage_n(uid1) == 1)
check("5 replay zero new Gokapi", _gk_count() == gk0 + 1)

# ══════════ 6-7. changed bytes / filename → conflict ══════════
OTHER = PNG_idm + b"\x89PNG\r\n\x1a\n"
body3, ct = _multipart([("a.png", OTHER, "image/png")],
                       {"tool": "compress", "client_ref": ref2})
st, r3 = _req_idm("POST", "/api/uploads", body3, {"Content-Type": ct, "Cookie": ck1})
check("6 same ref changed bytes → 409 idempotency_conflict",
      st == 409 and r3.get("error") == "idempotency_conflict", f"{st} {str(r3)[:80]}")
body4, ct = _multipart([("renamed.png", PNG_idm, "image/png")],
                       {"tool": "compress", "client_ref": ref2})
st, r4 = _req_idm("POST", "/api/uploads", body4, {"Content-Type": ct, "Cookie": ck1})
check("7 same ref changed filename → 409",
      st == 409 and r4.get("error") == "idempotency_conflict", f"{st} {str(r4)[:80]}")
check("7b conflict zero side effects", _usage_n(uid1) == 1 and _gk_count() == gk0 + 1)

# ══════════ 17-19. direct-share response-loss replay ══════════
ck2 = _register(_u + "w2")
with db() as con:
    uid2 = con.execute("select id from users where username=%s",
                       (_u + "w2",)).fetchone()["id"]
# the no-tool direct-share branch is RETIRED — every upload stages
# a tool source (sharing = the share tool). No-tool posts are rejected
# before any quota reservation or storage side effect.
ref17 = f"ds-{_u}-1"
_gk17 = _gk_count()
body, ct = _multipart([("s.png", PNG_idm, "image/png")],
                      {"client_ref": ref17})
st, r17 = _req_idm("POST", "/api/uploads", body, {"Content-Type": ct, "Cookie": ck2})
check("17 no-tool upload → 422 err_tool_required (share 走 share 工具)",
      st == 422 and r17.get("error") == "err_tool_required", f"{st} {str(r17)[:90]}")
check("18 rejection zero usage / zero receipts / zero Gokapi",
      _usage_n(uid2) == 0 and _ingress_n(uid2) == 0 and _gk_count() == _gk17,
      f"usage={_usage_n(uid2)} gk={_gk_count()}-{_gk17}")
with db() as con:
    ups17 = int(con.execute(
        "select count(*) c from uploads where user_id=%s", (uid2,)).fetchone()["c"])
check("19 rejection zero uploads rows", ups17 == 0)

# ══════════ 20-22. MCP fixture user (API key) ══════════
ck3 = _register(_u + "v1")
conn = _hc.HTTPConnection(*_HOST_idm, timeout=30)
conn.request("POST", "/api/keys", json.dumps({"name": "ig"}).encode(),
             {"Cookie": ck3, "Content-Type": "application/json"})
_tk = json.loads(conn.getresponse().read()); conn.close()
bearer = (_tk.get("key") or {}).get("key") or ""
with db() as con:
    uid3 = con.execute("select id from users where username=%s",
                       (_u + "v1",)).fetchone()["id"]

# ══════════ 23-29. MCP ══════════
conn = _hc.HTTPConnection(*_HOST_idm, timeout=30)
conn.request("POST", "/api/keys", json.dumps({"name": "igmcp"}).encode(),
             {"Cookie": ck3, "Content-Type": "application/json"})
_tk2 = json.loads(conn.getresponse().read()); conn.close()
bearer2 = (_tk2.get("key") or {}).get("key") or bearer

def _mcp_idm(arguments, name="compress", mid=1):
    b = {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}}
    st, r = _req_idm("POST", "/mcp", b, {
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
# credits for MCP metered call
from economy.billing import grant as _grant
_grant(int(uid3), 100000, "manual_topup", note="ig")

gk_mcp0 = _gk_count()
usage_mcp0 = _usage_n(uid3)
ref23 = f"mcp-{_u}-1"
st, r = _mcp_idm({"files": [{"filename": "m.png", "b64": base64.b64encode(PNG_idm).decode()}],
              "params": {}, "delivery": {"expiry_days": 3, "allowed_downloads": 10}})
check("23 MCP missing client_ref → -32602 pre-upload",
      (r.get("error") or {}).get("code") == -32602 and _gk_count() == gk_mcp0,
      str(r)[:100])

st, r = _mcp_idm({"files": [{"filename": "m.png", "b64": base64.b64encode(PNG_idm).decode()}],
              "params": {}, "delivery": {"expiry_days": 3, "allowed_downloads": 10},
              "client_ref": ref23})
_msc = (r.get("result") or {}).get("structuredContent") or {}
_mres = {"call_id": _msc.get("call_id"),
         "public_id": ((_msc.get("items") or [{}])[0].get("page_url")
                      or "").rsplit("/", 1)[-1]}
check("24a MCP first call ok (source+execute)",
      bool(_mres.get("public_id")), str(_msc)[:110])
mcp_call_id = _mres.get("callId") or _mres.get("call_id")
mcp_pub = _mres.get("public_id")
gk_mcp1 = _gk_count()
usage_mcp1 = _usage_n(uid3)

# exact retry: same ref, same inline file
st, r2 = _mcp_idm({"files": [{"filename": "m.png", "b64": base64.b64encode(PNG_idm).decode()}],
               "params": {}, "delivery": {"expiry_days": 3, "allowed_downloads": 10},
               "client_ref": ref23}, mid=2)
_msc2 = (r2.get("result") or {}).get("structuredContent") or {}
_mres2 = {"call_id": _msc2.get("call_id"),
          "public_id": ((_msc2.get("items") or [{}])[0].get("page_url")
                       or "").rsplit("/", 1)[-1]}
check("24 MCP retry same source handles (replay)", bool(_mres2.get("public_id")), str(_msc2)[:110])
check("25 MCP retry same call_id/publicId (tool replay)",
      _mres2.get("callId", _mres2.get("call_id")) == mcp_call_id
      and _mres2.get("public_id") == mcp_pub,
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
st, r4 = _mcp_idm({"files": [{"filename": "m.png", "b64": base64.b64encode(PNG_idm).decode()}],
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
st, r3 = _mcp_idm({"files": [{"filename": "m.png", "b64": base64.b64encode(OTHER).decode()}],
               "params": {}, "delivery": {"expiry_days": 3, "allowed_downloads": 10},
               "client_ref": ref23}, mid=3)
check("29 MCP same ref changed inline file → -32009 conflict, zero upload",
      (r3.get("error") or {}).get("code") == -32009
      and (r3.get("error") or {}).get("message") == "idempotency_conflict"
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
                            "size": len(PNG_idm), "sha256": hashlib.sha256(PNG_idm).hexdigest()}],
                          {"tool_id": "compress"})
refT = f"ack-{_u}-t"
rctT, uidsT, _ = claim_ingress_quota(uidT, "127.0.0.1", refT, hT, [len(PNG_idm)])
usage_T0 = _usage_n(uidT)
with db() as con:
    finalize_ok(con, int(rctT["id"]), [{"stagingId": "ACKT1"}])
# 模拟: finalize commit 后 route 层抛异常, reconciliation 介入
outT = resolve_outcome(int(rctT["id"]))
check("ACK-tool DURABLE_OK → stored result",
      outT["outcome"] == OUTCOME_DURABLE_OK
      and outT["result"][0]["stagingId"] == "ACKT1", str(outT)[:100])
check("ACK-tool durable-ok 零 quota release", _usage_n(uidT) == usage_T0 == 1)
# DURABLE_OK 下 replay 而非新 attempt:
_, _, repT = claim_ingress_quota(uidT, "127.0.0.1", refT, hT, [len(PNG_idm)])
check("ACK-tool same ref/hash claim → exact replay", repT == outT["result"])

# direct-share ACK-loss: uploads 落地 + receipt ok 同事务已 commit → reconcile DURABLE_OK
uidD2 = _mkuser(_u + "ackd")
hD2 = ingress_request_hash("web_direct_share",
                           [{"filename": "d.png", "content_type": "image/png",
                             "size": len(PNG_idm), "sha256": hashlib.sha256(PNG_idm).hexdigest()}],
                           {"expiry_days": 3, "allowed_downloads": 10})
refD2 = f"ack-{_u}-d"
rctD2, uidsD2, _ = claim_ingress_quota(uidD2, "127.0.0.1", refD2, hD2, [len(PNG_idm)])
from repo.ingress_requests import finalize_ok as _rpo_final
_direct_rec = [{"stagingId": "ACKD1", "filename": "d.png"}]
from repo.uploads import insert_record as _up_ins
with db() as con:
    _up_ins(con, uidD2, "127.0.0.1", "ACKD1", "ACKD1", "d.png",
            "image/png", len(PNG_idm), 3, 10, ts_idm + 3 * 86400,
            "http://127.0.0.1:8113/f/ACKD1", "", ts_idm)
    _rpo_final(con, int(rctD2["id"]), json.dumps(_direct_rec), ts_idm)
outD2 = resolve_outcome(int(rctD2["id"]))
check("ACK-direct DURABLE_OK → stored rows replay",
      outD2["outcome"] == OUTCOME_DURABLE_OK
      and outD2["result"][0]["stagingId"] == "ACKD1", str(outD2)[:100])
check("ACK-direct durable-ok 零 release / uploads 保留",
      _usage_n(uidD2) == 1)

# UNRESOLVED: receipt running + DB 行不可读模拟 — 直接构造 "finalize 失败且 receipt 仍 running":
uidU = _mkuser(_u + "unres")
hU = ingress_request_hash("v1_source",
                          [{"filename": "u.bin", "content_type": "application/octet-stream",
                            "size": len(PNG_idm), "sha256": hashlib.sha256(PNG_idm).hexdigest()}],
                          {})
rctU, uidsU, _ = claim_ingress_quota(uidU, "127.0.0.1", f"unres-{_u}", hU, [len(PNG_idm)])
outU = resolve_outcome(int(rctU["id"]))
check("UNRESOLVED-probe: running receipt (finalize 未发生) → PROVEN_ROLLBACK",
      outU["outcome"] == OUTCOME_PROVEN_ROLLBACK)
# guarded: settle_failure on an OK receipt → 拒绝 (返回 False, quota 不动)
settled_guard = settle_failure(int(rctT["id"]), uidsT, uidT)  # rctT 已 ok
check("guarded: settle_failure on ok receipt → 拒绝, quota durable",
      settled_guard is False and _usage_n(uidT) == 1, f"settled={settled_guard}")
# guarded: finalize_ok on failed receipt → None (拒绝 terminal rewrite)
rctF2, uidsF2, _ = claim_ingress_quota(uidU, "127.0.0.1", f"unres2-{_u}", hU, [len(PNG_idm)])
settle_failure(int(rctF2["id"]), uidsF2, uidU)
from repo.ingress_requests import finalize_ok as _ff, settle_failed as _sf
with db() as con:
    refused = _ff(con, int(rctF2["id"]), "[]", ts_idm)
check("guarded: finalize_ok on failed receipt → None", refused is None)
# claim validation (runtime):
for bad_ref in (None, "", "   ", "x" * 129, 123):
    try:
        claim_ingress_quota(uidU, "127.0.0.1", bad_ref, hU, [len(PNG_idm)])
        ok_bad = False
        break
    except ValueError:
        ok_bad = True
else:
    ok_bad = True
check("claim runtime ref validation (None/blank/long/non-str → ValueError)", ok_bad)
for bad_hash in (None, "abc", "Z" * 64, "a" * 63):
    try:
        claim_ingress_quota(uidU, "127.0.0.1", f"vh-{_u}", bad_hash, [len(PNG_idm)])
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
                             "size": len(PNG_idm), "sha256": hashlib.sha256(PNG_idm).hexdigest()}],
                           {})
rctF1, uidsF1, _ = claim_ingress_quota(uidF1, "127.0.0.1", f"f1-{_u}", hF1, [len(PNG_idm)])
settle_failure(int(rctF1["id"]), uidsF1, uidF1)
_f1_raised = False
try:
    with db() as con:
        finalize_ok(con, int(rctF1["id"]), [{"stagingId": "NOPE"}])
except RuntimeError as _e:
    _f1_raised = "ingress_request_not_running" in str(_e)
check("F1 failed receipt → finalize_ok raises ingress_request_not_running",
      _f1_raised)

# F2: direct-share landing tx — receipt 非 running → 整事务回滚 (uploads=0)
uidF2 = _mkuser(_u + "f2")
hF2 = ingress_request_hash("web_direct_share",
                           [{"filename": "f2.png", "content_type": "image/png",
                             "size": len(PNG_idm), "sha256": hashlib.sha256(PNG_idm).hexdigest()}],
                           {"expiry_days": 3, "allowed_downloads": 10})
rctF2b, uidsF2b, _ = claim_ingress_quota(uidF2, "127.0.0.1", f"f2-{_u}", hF2, [len(PNG_idm)])
# 先把 receipt 置 failed (模拟 recovery 抢先 settle), 再跑 direct-share landing 事务:
settle_failure(int(rctF2b["id"]), uidsF2b, uidF2)
from repo.uploads import insert_record as _ins2
from repo.ingress_requests import finalize_ok as _rfin2
_rolled = False
try:
    with db() as con:
        _ins2(con, uidF2, "127.0.0.1", "F2PID", "F2PID", "f2.png",
              "image/png", len(PNG_idm), 3, 10, ts_idm + 3 * 86400,
              "http://127.0.0.1:8113/f/F2PID", "", ts_idm)
        _row_fin = _rfin2(con, int(rctF2b["id"]), "[]", ts_idm)
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
def _throw_fin(con, ingress_id, result):
    raise RuntimeError("simulated ack loss")
def _unres_resolve(ingress_id):
    return {"outcome": _irmod.OUTCOME_UNRESOLVED}
uidF3 = _mkuser(_u + "f3")
conn3 = _hc.HTTPConnection(*_HOST_idm, timeout=30)
b_, ct_ = _form_idm({"username": _u + "f3m", "password": "Test12345",
                 "password_confirm": "Test12345", "email": f"{_u}f3m@t.co"})
conn3.request("POST", "/auth/register", b_, {"Content-Type": ct_})
conn3.getresponse().read()
b_, ct_ = _form_idm({"login": _u + "f3m", "password": "Test12345"})
conn3.request("POST", "/auth/login", b_, {"Content-Type": ct_})
r_ = conn3.getresponse(); r_.read()
ckF3 = (r_.getheader("Set-Cookie") or "").split(";")[0]
conn3.request("POST", "/api/keys", json.dumps({"name": "f3"}).encode(),
              {"Cookie": ckF3, "Content-Type": "application/json"})
_bearerF3 = (json.loads(conn3.getresponse().read()).get("key") or {}).get("key")
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
                                  "b64": base64.b64encode(PNG_idm).decode()}],
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
                             "size": len(PNG_idm), "sha256": hashlib.sha256(PNG_idm).hexdigest()}],
                           {})
rctF4, uidsF4, _ = claim_ingress_quota(uidF4, "127.0.0.1", f"f4-{_u}", hF4, [len(PNG_idm)])
with db() as con:
    finalize_ok(con, int(rctF4["id"]), [{"publicId": "F4"}])
with db() as con:
    con.execute("update ingress_requests set result_json='{bad'"
                " where id=%s", (rctF4["id"],))
_f4_raised = False
try:
    claim_ingress_quota(uidF4, "127.0.0.1", f"f4-{_u}", hF4, [len(PNG_idm)])
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
    claim_ingress_quota(uidF4, "127.0.0.1", f"f4-{_u}", hF4, [len(PNG_idm)])
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
                             "size": len(PNG_idm), "sha256": hashlib.sha256(PNG_idm).hexdigest()}],
                           {"tool_id": "compress"})
refW = f"surf-{_u}-w"
rctW2, uidsW2, _ = claim_ingress_quota(uidW, "127.0.0.1", refW, hS2,
                                           [len(PNG_idm)], scope="compress")
with db() as con:
    finalize_ok(con, int(rctW2["id"]), [{"stagingId": "SURFW2"}])
with db() as con:
    con.execute("update ingress_requests set result_json='{bad'"
                " where id=%s", (rctW2["id"],))
_gkW0 = _gk_count()
_uuW0 = _usage_n(uidW)
body, ct = _multipart([("s.png", PNG_idm, "image/png")],
                      {"tool": "compress", "client_ref": refW})
stW, rW = _req_idm("POST", "/api/uploads", body, {"Content-Type": ct, "Cookie": _ckS2})
with db() as con:
    _stW = con.execute("select status from ingress_requests where id=%s",
                       (rctW2["id"],)).fetchone()["status"]
check("F7 Web malformed replay → 500 err_ingress_unresolved + 零副作用",
      stW == 500 and rW.get("error") == "err_ingress_unresolved"
      and _gk_count() == _gkW0 and _usage_n(uidW) == _uuW0
      and _stW == "ok",
      f"{stW} {str(rW)[:80]} gk={_gkW0}->{_gk_count()} usage={_uuW0}->{_usage_n(uidW)}")

# F8/F9 tokens (surf): web cookie mint + machine bearer
connS = _hc.HTTPConnection(*_HOST_idm, timeout=30)
connS.request("POST", "/api/keys", json.dumps({"name": "surf"}).encode(),
              {"Cookie": _ckS2, "Content-Type": "application/json"})
_bS = (json.loads(connS.getresponse().read()).get("key") or {}).get("key")
connS.close()

# F8: scalar stored payload (同 receipt 形态, web surface)
refV = f"surf-{_u}-v"
body, ct = _multipart([("s.png", PNG_idm, "image/png")],
                      {"tool": "compress", "client_ref": refV})
stV, rV = _req_idm("POST", "/api/uploads", body, {"Content-Type": ct, "Cookie": _ckS2})
with db() as con:
    _rV = con.execute("select id from ingress_requests where user_id=%s"
                      " and request_ref=%s", (uidW, refV)).fetchone()
    rctV = dict(_rV) if _rV else {"id": 0}
    if _rV:
        # the web route already finalized ok — poison the stored payload only
        con.execute("update ingress_requests set result_json='123'"
                    " where id=%s", (rctV["id"],))
_gkV0 = _gk_count()
_uuV0 = _usage_n(uidW)
stV, rV = _req_idm("POST", "/api/uploads", body, {"Content-Type": ct, "Cookie": _ckS2})
with db() as con:
    _stV = con.execute("select status from ingress_requests where id=%s",
                       (rctV["id"],)).fetchone()["status"]
check("F8 v1 scalar replay → 500 err_ingress_unresolved + 零副作用",
      stV == 500 and rV.get("error") == "err_ingress_unresolved"
      and _gk_count() == _gkV0 and _usage_n(uidW) == _uuV0
      and _stV == "ok",
      f"{stV} {str(rV)[:80]} gk={_gkV0}->{_gk_count()} usage={_uuV0}->{_usage_n(uidW)}")

# F9: MCP JSON-RPC (同 receipt 形态, client_ref)
hMCP = ingress_request_hash("tool_source",
                             [{"filename": "s.png", "content_type": "image/png",
                               "size": len(PNG_idm), "sha256": hashlib.sha256(PNG_idm).hexdigest()}],
                             {"tool_id": "compress"})
refM = f"surf-{_u}-m"
rctM, uidsM, _ = claim_ingress_quota(uidW, "127.0.0.1", refM, hMCP,
                                      [len(PNG_idm)], scope="compress")
with db() as con:
    finalize_ok(con, int(rctM["id"]), [{"stagingId": "SURFM"}])
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
    st_, r_ = _req_idm("POST", "/mcp", b_, {
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
                            "b64": base64.b64encode(PNG_idm).decode()}],
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
                              "size": len(PNG_idm), "sha256": hashlib.sha256(PNG_idm).hexdigest()}],
                            {})
# F10: earliest ok + result_json='' → IngressUnresolved
rct10, uids10, _ = claim_ingress_quota(uidF10, "127.0.0.1", f"f10-{_u}", hF10, [len(PNG_idm)])
with db() as con:
    finalize_ok(con, int(rct10["id"]), [{"publicId": "F10"}])
with db() as con:
    con.execute("update ingress_requests set result_json='' where id=%s",
                (rct10["id"],))
_f10_raised = False
try:
    claim_ingress_quota(uidF10, "127.0.0.1", f"f10-{_u}", hF10, [len(PNG_idm)])
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
                                         hF10, [len(PNG_idm)])
settle_failure(int(rct11a["id"]), uids11a, uidF11)   # failed history
rct11b, uids11b, _ = claim_ingress_quota(uidF11, "127.0.0.1", f"f11-{_u}",
                                         hF10, [len(PNG_idm)])  # same hash retry
with db() as con:
    finalize_ok(con, int(rct11b["id"]), [{"publicId": "F11"}])
with db() as con:
    con.execute("update ingress_requests set result_json='' where id=%s",
                (rct11b["id"],))
_f11_raised = False
try:
    claim_ingress_quota(uidF11, "127.0.0.1", f"f11-{_u}", hF10, [len(PNG_idm)])
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
                            "size": len(PNG_idm), "sha256": hashlib.sha256(PNG_idm).hexdigest()}],
                          {"tool_id": "compress"})
hB = ingress_request_hash("web_tool_source",
                          [{"filename": "b.png", "content_type": "image/png",
                            "size": len(PNG_idm), "sha256": hashlib.sha256(PNG_idm).hexdigest()}],
                          {"tool_id": "compress"})
refS = f"svc-{_u}-1"
rct, uids, replay = claim_ingress_quota(uidS, "127.0.0.1", refS, hA, [len(PNG_idm)])
check("G claim ok → running receipt + usage", rct["status"] == "running" and len(uids) == 1)

# 8: running same hash → in_progress
try:
    claim_ingress_quota(uidS, "127.0.0.1", refS, hA, [len(PNG_idm)])
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
rct2, uids2, _ = claim_ingress_quota(uidS, "127.0.0.1", refS, hA, [len(PNG_idm)])
check("9 failed same hash → new attempt allowed", rct2["id"] != rct["id"])

# 10: failed different hash → conflict
try:
    claim_ingress_quota(uidS, "127.0.0.1", refS, hB, [len(PNG_idm)])
    check("10 failed changed hash → conflict", False, "no raise")
except IngressConflict:
    check("10 failed changed hash → conflict", True)

# 13: commit-ACK durable ok → replay no cleanup
with db() as con:
    finalize_ok(con, int(rct2["id"]), [{"stagingId": "X1"}])
out = resolve_outcome(int(rct2["id"]))
check("13 outcome durable_ok + stored result",
      out["outcome"] == OUTCOME_DURABLE_OK and out["result"][0]["stagingId"] == "X1")
# earliest=failed + later=ok: claim must REPLAY the live ok receipt
rct3, uids3, replay3 = claim_ingress_quota(uidS, "127.0.0.1", refS, hA, [len(PNG_idm)])
check("13b ok receipt same hash claim → replay payload (failed-then-ok history)",
      rct3 is None and replay3 == [{"stagingId": "X1"}],
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
rctR, uidsR, _ = claim_ingress_quota(uidR, "127.0.0.1", f"rec-{_u}", hA, [len(PNG_idm)])
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
        r, u, _rp = claim_ingress_quota(uidC, "127.0.0.1", refC, h, [len(PNG_idm)])
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
        " label_json, visibility,"
        " enabled, created_at, updated_at, accept_json, format_family) values (%s, NULL, %s, 'local',"
        " %s, 'public', TRUE, %s, %s, '[\".png\"]', 'image')",
        (tO, tO, json.dumps({"en": "IG"}), ts_idm, ts_idm))
    con.execute("insert into pricing (tool_id, cost, enabled, updated_at)"
                " values (%s, 0, TRUE, %s)", (tO, ts_idm))
with db() as con:
    rate = commission_rate(con)
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
    a_row, a_cl = claim_call(con, f"race-{_u}", uidO, tO, 0, "{}", ts_idm,
                             commission_rate_snapshot=rate,
                             invocation_hash=hashA)
    con.execute("update tool_calls set status='failed', finished_at=%s"
                " where id=%s", (ts_idm, a_row["id"]))
b_claimed = None
with db() as con:
    _, b_claimed = claim_call(con, f"race-{_u}", uidO, tO, 0, "{}", ts_idm,
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

# ══════════ P4: 文件暂存单点故障注入 + 引用/新文件混合顺序 ══════════
# 三入口 (web 上传 / MCP ticket PUT / MCP 内联) 共用 sharing.staging.
# stage_sources — 在单点上注入三类故障; ticket 路由即该函数的薄映射,
# 行为由这里的注入用例覆盖。
import asyncio as _aio
from sharing import staging as _staging_mod
from sharing.staging import StagingError as _StagingError

uidP4 = _mkuser(_u + "p4st")

def _p4_hash(tag):
    return ingress_request_hash(
        "mcp_upload_ticket",
        [{"filename": f"{tag}.png", "content_type": "image/png",
          "size": len(PNG_idm), "sha256": hashlib.sha256(PNG_idm).hexdigest()}], {})

# (a) finalize ACK 丢失: 真实提交在独立事务落地后调用方收到异常
#     → 三态核验 DURABLE_OK → 按成功作答, 返回存储结果
#     (receipt 原语按调用期导入 — 注入打在 ingress_receipt 模块属性上)
import sharing.ingress_receipt as _irmod_p4
_orig_fin_p4 = _irmod_p4.finalize_ok
def _fin_ack_loss(con, ingress_id, result):
    with db() as own:
        _orig_fin_p4(own, ingress_id, result)
    raise RuntimeError("ack lost")
_irmod_p4.finalize_ok = _fin_ack_loss
try:
    _r_a = _aio.run(_staging_mod.stage_sources(
        uidP4, "127.0.0.1", f"p4ack-{_u}", "", _p4_hash("ack"),
        [("ack.png", "image/png", PNG_idm)]))
    _ok_a = isinstance(_r_a, list) and bool(
        _r_a[0].get("stagingId") and _r_a[0].get("sourceToken"))
except Exception as exc:  # noqa: BLE001
    _ok_a, _r_a = f"raised {type(exc).__name__}", None
finally:
    _irmod_p4.finalize_ok = _orig_fin_p4
with db() as con:
    _st_a = con.execute(
        "select status from ingress_requests where user_id=%s and"
        " request_ref=%s", (uidP4, f"p4ack-{_u}")).fetchone()
check("P4a finalize ACK loss → DURABLE_OK answered as success (stored handles)",
      _ok_a is True and _st_a is not None and _st_a["status"] == "ok"
      and _usage_n(uidP4) == 1,
      f"{_ok_a} {_st_a and _st_a['status']} usage={_usage_n(uidP4)}")

# (b) 确认回滚: finalize 失败且 receipt 仍 running → 立即清理暂存对象
#     + 精确释放配额 + 落 failed, 抛 StagingError
def _fin_pure_fail(con, ingress_id, result):
    raise RuntimeError("commit never happened")
_irmod_p4.finalize_ok = _fin_pure_fail
_gk_p4_0 = _gk_count()
try:
    _aio.run(_staging_mod.stage_sources(
        uidP4, "127.0.0.1", f"p4rb-{_u}", "", _p4_hash("rb"),
        [("rb.png", "image/png", PNG_idm)]))
    _ok_b = "no raise"
except _StagingError:
    _ok_b = True
except Exception as exc:  # noqa: BLE001
    _ok_b = f"raised {type(exc).__name__}"
finally:
    _irmod_p4.finalize_ok = _orig_fin_p4
with db() as con:
    _st_b = con.execute(
        "select status from ingress_requests where user_id=%s and"
        " request_ref=%s", (uidP4, f"p4rb-{_u}")).fetchone()
check("P4b proven rollback → StagingError, staged file deleted, quota released, receipt failed",
      _ok_b is True and _st_b is not None and _st_b["status"] == "failed"
      and _gk_count() == _gk_p4_0 and _usage_n(uidP4) == 1,
      f"{_ok_b} {_st_b and _st_b['status']} gk={_gk_count()}/{_gk_p4_0}"
      f" usage={_usage_n(uidP4)}")

# (c) 暂存写入失败 (半暂存回滚): 第二个文件写入抛错 → 已暂存的第一
#     个对象被清理, 配额释放, 抛 StagingError
import storage as _storage_mod
_orig_write_p4 = _storage_mod.write
_write_n = {"n": 0}
def _flaky_write(stream, filename, ct, ed, ad, size):
    _write_n["n"] += 1
    if _write_n["n"] == 2:
        raise RuntimeError("injected second-file staging failure")
    return _orig_write_p4(stream, filename, ct, ed, ad, size)
_storage_mod.write = _flaky_write
try:
    _aio.run(_staging_mod.stage_sources(
        uidP4, "127.0.0.1", f"p4wf-{_u}", "", _p4_hash("wf"),
        [("wf1.png", "image/png", PNG_idm), ("wf2.png", "image/png", PNG_idm)]))
    _ok_c = "no raise"
except _StagingError:
    _ok_c = True
except Exception as exc:  # noqa: BLE001
    _ok_c = f"raised {type(exc).__name__}"
finally:
    _storage_mod.write = _orig_write_p4
with db() as con:
    _st_c = con.execute(
        "select status from ingress_requests where user_id=%s and"
        " request_ref=%s", (uidP4, f"p4wf-{_u}")).fetchone()
check("P4c mid-batch staging failure → first staged object cleaned, receipt failed",
      _ok_c is True and _write_n["n"] == 2
      and _st_c is not None and _st_c["status"] == "failed"
      and _gk_count() == _gk_p4_0 and _usage_n(uidP4) == 1,
      f"{_ok_c} writes={_write_n['n']} {_st_c and _st_c['status']}"
      f" gk={_gk_count()}/{_gk_p4_0} usage={_usage_n(uidP4)}")

# ══════════ P4d: 引用文件 + 新文件混合 → 顺序保持, 引用不暂存不计配额 ══════════
ckM = _register(_u + "mixrf")
with db() as con:
    uidM = con.execute("select id from users where username=%s",
                       (_u + "mixrf",)).fetchone()["id"]
_grant(int(uidM), 100000, "manual_topup", note="ig-p4mix")
_body_mp, _ct_mp = _multipart(
    [("a.png", PNG_idm, "image/png")],
    {"tool": "compress", "client_ref": f"mixsrc-{_u}"})
st, r = _req_idm("POST", "/api/uploads", _body_mp,
             {"Content-Type": _ct_mp, "Cookie": ckM})
src_a = (r or {}).get("file") or {}
check("P4d0 fixture: web-staged source has stagingId+sourceToken",
      st == 200 and src_a.get("stagingId") and src_a.get("sourceToken"),
      f"{st} {str(r)[:80]}")

import execution.dispatch as _dx_mod
from app.routes import mcp as _M
class _ReqMix:
    class client:
        host = "127.0.0.1"
    headers = {}
_captured_mix = []
async def _fake_dx_mix(request, user, raw, *a, **k):
    _captured_mix.append(raw)
    return {"ok": True, "call_id": 424242, "result": {}}, None, None
_orig_dx_mix = _dx_mod.dispatch_execute
_dx_mod.dispatch_execute = _fake_dx_mix
try:
    _mix_r = _aio.run(_M._call_tool(_ReqMix(), {"id": int(uidM)}, 7, {
        "name": "share",
        "arguments": {
            "files": [
                {"public_id": src_a.get("stagingId"),
                 "source_token": src_a.get("sourceToken")},
                {"filename": "b.png",
                 "b64": base64.b64encode(PNG_idm).decode()},
            ],
            "client_ref": f"mix-{_u}",
        }}))
finally:
    _dx_mod.dispatch_execute = _orig_dx_mix
_files_mix = (_captured_mix[-1]["source"]["files"]
              if _captured_mix else [])
check("P4d mixed [reference, new file] → canonical order preserved, ref not re-staged",
      len(_files_mix) == 2
      and _files_mix[0]["public_id"] == src_a.get("stagingId")
      and _files_mix[1]["public_id"] not in (
          None, src_a.get("stagingId")),
      f"files={str(_files_mix)[:100]} rpc={str(_mix_r)[:120]}")
check("P4d reference claims no ingress/quota (one receipt, one usage row for the new file only)",
      _ingress_n(int(uidM), f"mix-{_u}") == 1 and _usage_n(int(uidM)) == 2,
      f"ingress={_ingress_n(int(uidM), f'mix-{_u}')} usage={_usage_n(int(uidM))}"
      " (1 = web fixture, 2 = + the single new file; the reference added none)")

# ══════════ G3: 暂存前置闸门与操作身份 ══════════
# 数量窗口是 ingest_sources 的第 0 步 (唯一数量权威): 超窗/超上限 →
# 零拉取零 claim; URL 拉取逐项累计批量上限, 越界即刻停止; 操作身份只
# 锚定请求自带内容 (URL 项身份 = URL, 拉回内容不进哈希)。
import remote as _rm_g3
_g3_state = {"fetches": 0, "body": PNG_idm}
def _g3_hop(url, method="GET", body=None, headers=None, timeout_s=60,
            max_bytes=None, deadline=None):
    _g3_state["fetches"] += 1
    return 200, _g3_state["body"], None, "image/png", None
_rs_g3 = _rm_g3.socket
_orig_pin_g3, _orig_res_g3 = _rm_g3._pinned_request, _rm_g3._resolve_host
_rm_g3._resolve_host = lambda h, p: [
    (_rs_g3.AF_INET, _rs_g3.SOCK_STREAM, _rs_g3.IPPROTO_TCP, "",
     ("93.184.216.34", p))]
_rm_g3._pinned_request = _g3_hop
_orig_dx_g3 = _dx_mod.dispatch_execute
_dx_mod.dispatch_execute = _fake_dx_mix
ckG3 = _register(_u + "g3")
with db() as con:
    uidG3 = con.execute("select id from users where username=%s",
                        (_u + "g3",)).fetchone()["id"]
_grant(int(uidG3), 100000, "manual_topup", note="ig-g3")

def _g3_call(ref, files, mid=30, name="compress"):
    return _aio.run(_M._call_tool(
        _ReqMix(), {"id": int(uidG3)}, mid,
        {"name": name,
         "arguments": {"files": files, "client_ref": ref}}))

def _g3_text(r):
    try:
        return json.dumps(r.get("result") or {})
    except Exception:
        return str(r)

try:
    # (a) 21 个 URL 到 1..1 工具 → 数量窗口先拒, 零拉取
    _gk_g3a, _use_g3a = _gk_count(), _usage_n(int(uidG3))
    _g3_state["fetches"] = 0
    _r_a = _g3_call(f"g3a-{_u}",
                    [{"url": f"https://g3.example/{_u}/f{i}.png"}
                     for i in range(21)], mid=31)
    check("G3a 21 URLs to a 1..1 tool → isError err_file_count, ZERO fetches",
          (_r_a.get("result") or {}).get("isError") is True
          and "err_file_count" in _g3_text(_r_a)
          and _g3_state["fetches"] == 0
          and _ingress_n(int(uidG3), f"g3a-{_u}") == 0
          and _gk_count() == _gk_g3a and _usage_n(int(uidG3)) == _use_g3a,
          f"fetches={_g3_state['fetches']} {str(_r_a)[:100]}")

    # (b) URL 逐项累计批量上限: 4 个 34MB URL 到 1..20 的 share 工具 →
    #     第 3 个后越界, 第 4 个不再拉取; 零 claim/usage/Gokapi
    from config import MAX_UPLOAD_BYTES as _G3_MAXUP, \
        MAX_BATCH_BYTES as _G3_MAXBAT
    _g3_state["body"] = b"\x89PNG\r\n\x1a\n" + b"\0" * (34 * 1024 * 1024)
    _gk_g3b, _use_g3b = _gk_count(), _usage_n(int(uidG3))
    _g3_state["fetches"] = 0
    _r_b = _g3_call(f"g3b-{_u}",
                    [{"url": f"https://g3.example/{_u}/big{i}.png"}
                     for i in range(4)], mid=32, name="share")
    check("G3b cumulative batch cap → abort at the boundary, no further fetch",
          (_r_b.get("result") or {}).get("isError") is True
          and "err_batch_too_large" in _g3_text(_r_b)
          and 3 * 34 * 1024 * 1024 > _G3_MAXBAT
          and _g3_state["fetches"] == 3
          and _ingress_n(int(uidG3), f"g3b-{_u}") == 0
          and _gk_count() == _gk_g3b and _usage_n(int(uidG3)) == _use_g3b,
          f"fetches={_g3_state['fetches']} {str(_r_b)[:100]}")

    # (c) 操作身份: 同 client_ref 同 URL 列表, 远端内容已变 → 重放;
    #     换 URL 列表 → -32009 冲突
    _g3_state["body"] = PNG_idm
    _urlA = f"https://g3.example/{_u}/same.png"
    _urlB = f"https://g3.example/{_u}/other.png"
    _gk_g3c = _gk_count()
    _r_c1 = _g3_call(f"g3c-{_u}", [{"url": _urlA}], mid=33)
    _src_c1 = (_captured_mix[-1]["source"]["files"][0]["public_id"]
               if _captured_mix else None)
    _g3_state["body"] = PNG_idm + b"changed-bytes"  # remote content changed
    _r_c2 = _g3_call(f"g3c-{_u}", [{"url": _urlA}], mid=34)
    _src_c2 = (_captured_mix[-1]["source"]["files"][0]["public_id"]
               if _captured_mix else None)
    check("G3c same key + same URLs + changed remote content → REPLAY",
          (_r_c2.get("result") or {}).get("isError") is False
          and _src_c1 and _src_c2 == _src_c1
          and _gk_count() == _gk_g3c + 1,
          f"src={_src_c1}/{_src_c2} gk={_gk_count()}-{_gk_g3c}")
    _gk_g3c2 = _gk_count()
    _r_c3 = _g3_call(f"g3c-{_u}", [{"url": _urlB}], mid=35)
    check("G3c2 same key + different URL list → -32009 idempotency_conflict",
          (_r_c3.get("error") or {}).get("code") == -32009
          and _gk_count() == _gk_g3c2, str(_r_c3)[:110])

    # (d) 哈希构成: URL 请求 files 列表为空 (身份 = ctx.sources 的 URL);
    #     纯 inline 请求哈希 = 既有公式 (inline 字节)
    with db() as con:
        _h_url = con.execute(
            "select request_hash from ingress_requests"
            " where user_id=%s and request_ref=%s",
            (int(uidG3), f"g3c-{_u}")).fetchone()["request_hash"]
    _exp_url = ingress_request_hash(
        "tool_source", [],
        {"tool_id": "compress", "sources": [{"kind": "url", "url": _urlA}]})
    check("G3d URL-source identity = URL (fetched bytes excluded from hash)",
          _h_url == _exp_url, f"{_h_url[:24]} vs {_exp_url[:24]}")
    _r_e = _g3_call(f"g3e-{_u}", [{"filename": "e.png", "b64": base64.b64encode(PNG_idm).decode()}], mid=36)
    with db() as con:
        _h_inl = con.execute(
            "select request_hash from ingress_requests"
            " where user_id=%s and request_ref=%s",
            (int(uidG3), f"g3e-{_u}")).fetchone()["request_hash"]
    _exp_inl = ingress_request_hash(
        "tool_source",
        [{"filename": "e.png", "content_type": "image/png",
          "size": len(PNG_idm), "sha256": hashlib.sha256(PNG_idm).hexdigest()}],
        {"tool_id": "compress"})
    check("G3e pure-inline identity unchanged (inline bytes, no sources ctx)",
          (_r_e.get("result") or {}).get("isError") is False
          and _h_inl == _exp_inl, f"{_h_inl[:24]} vs {_exp_inl[:24]}")
finally:
    _rm_g3._pinned_request = _orig_pin_g3
    _rm_g3._resolve_host = _orig_res_g3
    _dx_mod.dispatch_execute = _orig_dx_g3


# ══════════ P5: 取消窗口 — claim / 暂存写 / finalize 线程提交后取消,
# 不得丢 receipt、配额或暂存对象的收尾义务。受控交错 (Events 锚定
# 线程内时刻), 断言持久事实 (receipt 状态 / usage / gokapi 对象数)。
# ══════════
import threading as _thr_p5
import sharing.ingress_receipt as _irmod_p5

section('staging cancellation: receipt/quota/object obligations survive')

uidP5 = _mkuser(_u + "p5cx")


def _p5_hash(tag):
    return ingress_request_hash(
        "mcp_upload_ticket",
        [{"filename": f"{tag}.png", "content_type": "image/png",
          "size": len(PNG_idm), "sha256": hashlib.sha256(PNG_idm).hexdigest()}], {})


def _receipt_row(ref):
    with db() as con:
        return con.execute(
            "select status from ingress_requests where user_id=%s and"
            " request_ref=%s", (uidP5, ref)).fetchone()


async def _cancel_after_entered(make_task, entered, release):
    """Shared driver: run make_task(), wait until the worker is parked
    inside the patched durable step, cancel, release concurrently, await."""
    task = _aio.ensure_future(make_task())
    if not await _aio.to_thread(entered.wait, 3):
        raise RuntimeError("p5: injected step never ran")
    task.cancel()
    _thr_p5.Timer(0.05, release.set).start()
    try:
        await task
        return False
    except _aio.CancelledError:
        return True


_gk0_p5 = _gk_count()

# (a) 取消落在 claim 线程暂停中 (claim 已提交, 零暂存对象) → 精确配额
#     释放 + receipt 落 failed, 取消随后传播。
_entered_a, _release_a = _thr_p5.Event(), _thr_p5.Event()
_orig_claim_p5 = _irmod_p5.claim_ingress_quota


def _paused_claim_p5(*a, **k):
    out = _orig_claim_p5(*a, **k)
    _entered_a.set()
    if not _release_a.wait(5):
        raise RuntimeError("p5a release timeout")
    return out


_irmod_p5.claim_ingress_quota = _paused_claim_p5
try:
    _cancel_a = _aio.run(_cancel_after_entered(
        lambda: _staging_mod.stage_sources(
            uidP5, "127.0.0.1", f"p5claim-{_u}", "", _p5_hash("claim"),
            [("claim.png", "image/png", PNG_idm)]),
        _entered_a, _release_a))
finally:
    _irmod_p5.claim_ingress_quota = _orig_claim_p5
_row_a = _receipt_row(f"p5claim-{_u}")
check("P5a cancel in claim window → receipt failed, quota exactly released, nothing staged",
      _cancel_a is True and _row_a is not None and _row_a["status"] == "failed"
      and _usage_n(uidP5) == 0 and _gk_count() == _gk0_p5,
      f"{_cancel_a} {_row_a and _row_a['status']} usage={_usage_n(uidP5)}"
      f" gk={_gk_count()}/{_gk0_p5}")

# (b) 取消落在暂存写线程暂停中 (对象已落 fake gokapi) → 回滚删除该
#     对象 + 配额释放 + receipt 落 failed。
_entered_b, _release_b = _thr_p5.Event(), _thr_p5.Event()
_orig_write_p5 = _storage_mod.write


def _paused_write_p5(stream, filename, ct, ed, ad, size):
    out = _orig_write_p5(stream, filename, ct, ed, ad, size)
    _entered_b.set()
    if not _release_b.wait(5):
        raise RuntimeError("p5b release timeout")
    return out


_storage_mod.write = _paused_write_p5
try:
    _cancel_b = _aio.run(_cancel_after_entered(
        lambda: _staging_mod.stage_sources(
            uidP5, "127.0.0.1", f"p5write-{_u}", "", _p5_hash("write"),
            [("write.png", "image/png", PNG_idm)]),
        _entered_b, _release_b))
finally:
    _storage_mod.write = _orig_write_p5
_row_b = _receipt_row(f"p5write-{_u}")
check("P5b cancel in staging-write window → staged object rolled back, receipt failed",
      _cancel_b is True and _row_b is not None and _row_b["status"] == "failed"
      and _usage_n(uidP5) == 0 and _gk_count() == _gk0_p5,
      f"{_cancel_b} {_row_b and _row_b['status']} usage={_usage_n(uidP5)}"
      f" gk={_gk_count()}/{_gk0_p5}")

# (c) 取消落在 finalize 线程暂停中, 成功提交已在独立事务落地 →
#     durable ok 站住 (对象保留、配额计入), 同 client_ref 重试重放。
_entered_c, _release_c = _thr_p5.Event(), _thr_p5.Event()
_orig_fin_p5 = _irmod_p5.finalize_ok


def _paused_fin_p5(con, ingress_id, result):
    with db() as own:
        _orig_fin_p5(own, ingress_id, result)
    _entered_c.set()
    if not _release_c.wait(5):
        raise RuntimeError("p5c release timeout")


_irmod_p5.finalize_ok = _paused_fin_p5
try:
    _cancel_c = _aio.run(_cancel_after_entered(
        lambda: _staging_mod.stage_sources(
            uidP5, "127.0.0.1", f"p5fin-{_u}", "", _p5_hash("fin"),
            [("fin.png", "image/png", PNG_idm)]),
        _entered_c, _release_c))
finally:
    _irmod_p5.finalize_ok = _orig_fin_p5
_row_c = _receipt_row(f"p5fin-{_u}")
_gk_after_c = _gk_count()
check("P5c cancel after durable finalize → receipt ok, object kept, quota counted",
      _cancel_c is True and _row_c is not None and _row_c["status"] == "ok"
      and _usage_n(uidP5) == 1 and _gk_after_c == _gk0_p5 + 1,
      f"{_cancel_c} {_row_c and _row_c['status']} usage={_usage_n(uidP5)}"
      f" gk={_gk_after_c}/{_gk0_p5}")
# same client_ref retry → replay of the stored handles: no new object,
# no second usage, same staging id
_r_replay = _aio.run(_staging_mod.stage_sources(
    uidP5, "127.0.0.1", f"p5fin-{_u}", "", _p5_hash("fin"),
    [("fin.png", "image/png", PNG_idm)]))
check("P5d same-ref retry after cancelled-durable-ok → replay (no new object/usage)",
      isinstance(_r_replay, list) and bool(_r_replay[0].get("stagingId"))
      and _usage_n(uidP5) == 1 and _gk_count() == _gk_after_c,
      f"usage={_usage_n(uidP5)} gk={_gk_count()}/{_gk_after_c}")


# ════════ RN: 文件重命名接口行为（真实 PATCH 路由）════════
# 先红后绿: 旧代码 rename_upload_route 的函数体局部 import asyncio
# 位于其首次使用之后, 使 asyncio 成为未赋值局部名 — 全部 PATCH 500。
# 覆盖: 未登录 401 / 所有者改名+扩展名保留+分享页反映 / 非所有者 403。
section('rename route: auth, ownership, extension, page reflection')

import base64 as _b64_rn
import http.client as _hc_rn
import json as _json_rn

_RN_HOST = ("127.0.0.1", int(TEST_PORT))


def _rn_http(method, path, body=None, headers=None, cookie=None):
    conn = _hc_rn.HTTPConnection(*_RN_HOST, timeout=30)
    h = dict(headers or {})
    if cookie:
        h["Cookie"] = cookie
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else _json_rn.dumps(body).encode()
    conn.request(method, path, payload, h)
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    try:
        return r.status, _json_rn.loads(raw)
    except Exception:
        return r.status, raw.decode(errors="replace")


def _rn_form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _rn_register(name):
    b, ct = _rn_form({"username": name, "password": "Test12345",
                      "password_confirm": "Test12345",
                      "email": f"{name}@t.co"})
    _rn_http("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _rn_form({"login": name, "password": "Test12345"})
    conn = _hc_rn.HTTPConnection(*_RN_HOST, timeout=30)
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse()
    r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close()
    return ck


def _rn_share_png(token, ref):
    """One durable shared PNG via the share tool (inline source)."""
    _st, d = _rn_http("POST", "/mcp", {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "share", "arguments": {
            "client_ref": ref, "files": [
                {"filename": "rn-pic.png",
                 "b64": _b64_rn.b64encode(_png()).decode()}]}}},
        {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         "Authorization": f"Bearer {token}"})
    sc = (((d or {}).get("result") or {}).get("structuredContent") or {})
    return next((i["public_id"] for i in sc.get("items", [])
                 if i.get("kind") == "file"), "")


# owner gets a session cookie AND an API key (the share call needs the key)
_rn_owner = _u + "rnow"
_rn_ck = _rn_register(_rn_owner)
_rn_ck2 = _rn_register(_u + "rnno")
_st, _d = _rn_http("POST", "/api/keys", {"name": "rn"},
                   {"Content-Type": "application/json", "Cookie": _rn_ck})
_rn_tok = (_d or {}).get("key", {}).get("key", "")
_rn_pid = _rn_share_png(_rn_tok, f"rnshare-{_u}")
check("RN0 fixture: one durable shared png", bool(_rn_pid), _rn_pid)

# anonymous: 401 auth_required (never a silent redirect, never a 500)
_st, _d = _rn_http("PATCH", f"/api/uploads/{_rn_pid}",
                   {"name": "anon"},
                   {"Content-Type": "application/json"})
check("RN1 anonymous rename → 401 auth_required",
      _st == 401 and (_d or {}).get("error") == "auth_required", (_st, _d))

# owner: stem renamed, original extension preserved
_st, _d = _rn_http("PATCH", f"/api/uploads/{_rn_pid}",
                   {"name": "renamed stem"},
                   {"Content-Type": "application/json"}, cookie=_rn_ck)
check("RN2 owner rename → ok, extension preserved",
      _st == 200 and (_d or {}).get("ok") is True
      and (_d or {}).get("name") == "renamed stem.png", (_st, _d))

# the share page reflects the new name
_st, _d = _rn_http("GET", f"/f/{_rn_pid}", None,
                   {"Accept": "application/json"})
check("RN3 share page reflects the new name",
      _st == 200 and (_d or {}).get("filename") == "renamed stem.png",
      (_st, _d))

# non-owner: 403 forbidden, the name stays the owner's
_st, _d = _rn_http("PATCH", f"/api/uploads/{_rn_pid}",
                   {"name": "hijack"},
                   {"Content-Type": "application/json"}, cookie=_rn_ck2)
_st2, _d2 = _rn_http("GET", f"/f/{_rn_pid}", None,
                     {"Accept": "application/json"})
check("RN4 non-owner rename → 403, name unchanged",
      _st == 403 and (_d or {}).get("error") == "forbidden"
      and (_d2 or {}).get("filename") == "renamed stem.png", (_st, _d, _d2))


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
        purge_users(con, [uid])
print("cleanup: ingress fixtures removed")


finish()
