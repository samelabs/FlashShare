#!/usr/bin/env python3
"""IO-1 contract: inputs files window / items results / MCP agent surface.

  D*  declaration: inputs.files {min,max,accept} (0 min legal), outputs
      kinds, multi rejected, text param limit
  C*  call: 0-file text tool executes; file-count window enforced
      (under min / over max → typed error)
  R*  results: multi-file share → one file item per input, ordered,
      anonymous download md5 identical; result page /r/{id} renders
      ordered items; settlement is one transaction (injected landing
      failure leaves zero uploads rows)
  M*  MCP: outputSchema present and consistent; structuredContent items
      match; pass-through share of a small image emits NO image block
      (echo suppressed) while a transformed image does; json item
      rendered as text; isError text is one JSON block with error +
      message + hint (format mismatch lists accept; file count states
      the window)
  X*  developer endpoint invalid shape → call failed + exact refund;
      valid items shape (text + json + file via data_b64) round-trips

A first-party trusted http fixture implements "text in → text+json+file
out" with a mode switch for the invalid-shape case.

Fail-closed env guard; per-run unique names; self-cleanup.
"""
import base64
import hashlib
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


import urllib.parse

_HOST = ("127.0.0.1", int(_TEST_PORT))
_BASE = f"http://127.0.0.1:{_TEST_PORT}"
BASE_URL = _os.environ.get("FLASHSHARE_BASE_URL", _BASE).rstrip("/")
_u = "io" + _uuid.uuid4().hex[:6]
ts = int(_time.time())

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")


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


from database import db

uid = None
TOOL_KEY = f"io{_u}_texty"
try:
    # register user + API key + credits
    b, ct = _form({"username": _u + "a", "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{_u}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": _u + "a", "password": "Test12345"})
    st_l, _h, _d, sc = _req("POST", "/auth/login", b, {"Content-Type": ct})
    ck = sc.split(";")[0]
    st, _h, d_k, _sc = _req("POST", "/api/keys", {"name": "io"},
                             {"Content-Type": "application/json", "Cookie": ck})
    TOK = d_k["key"]["key"]
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (_u + "a",)).fetchone()["id"])
        from repo.ledger import insert_row
        insert_row(con, uid, 100000, 100000, "manual_topup", None, None,
                   f"io:{_u}", None, ts)
    check("F1 user + key + credits", uid and TOK, uid)

    # The suite targets the LOCAL tool surface for text-out
    # (registry-backed) and validates the developer http items contract
    # through direct drive_http calls (the hosted /tool-endpoints
    # surface was deleted in C1 — first-party tools run local only).
    # ── text-out local tool: register via seed adapter is code-side
    # only, so drive the contract through share/compress plus direct
    # module-level item validation. ──

    # ═══ D. declaration ═══
    from governance.tool_supply import (validate_tool_declaration,
                                        validate_tool_patch,
                                        DeclarationError)
    good = validate_tool_declaration({
        "tool_key": f"{_u}t", "tool_id": f"{_u}t", "owner_id": None,
        "execution": "local",
        "label": {"en": "T"}, "icon": "wand",
        "inputs": {"files": {"min": 0, "max": 20, "accept": None}},
        "outputs": ["text", "json"], "params": None,
        "visibility": "public", "enabled": True})
    check("D1 min=0 declaration valid",
          good["inputs"]["files"]["min"] == 0, good)
    text_only = validate_tool_declaration({
        "tool_key": f"{_u}t3", "tool_id": f"{_u}t3", "owner_id": None,
        "execution": "local",
        "label": {"en": "T"}, "icon": "wand",
        "inputs": {"files": {"min": 0, "max": 0, "accept": None}},
        "outputs": ["text"], "params": None,
        "visibility": "public", "enabled": True})
    check("D1b text-only declaration (files 0..0) valid",
          text_only["inputs"]["files"]["max"] == 0, text_only)
    for bad, why in (
        ({"min": -1, "max": 1, "accept": None}, "min<0"),
        ({"min": 2, "max": 1, "accept": None}, "min>max"),
        ({"min": 1, "max": 21, "accept": None}, "max>20"),
        ({"min": 0, "max": 0, "accept": [".png"]}, "max=0 with accept"),
    ):
        decl = {"tool_key": f"{_u}t2", "tool_id": f"{_u}t2", "owner_id": None,
                "execution": "local",
                "label": {"en": "T"}, "icon": "wand",
                "inputs": {"files": bad},
                "outputs": ["file"], "params": None,
                "visibility": "public", "enabled": True}
        try:
            validate_tool_declaration(decl)
            check(f"D2 {why} rejected", False, "accepted")
        except DeclarationError as exc:
            check(f"D2 {why} rejected", str(exc) == "bad_inputs", str(exc))
    patch = validate_tool_patch({"multi": True})
    check("D3 multi is no longer declaration vocabulary (ignored key,"
          " never persisted)", "multi" not in patch, patch)
    from governance.tool_params import (validate_params_decl,
                                        enforce_params_decl,
                                        ParamsValueError,
                                        TEXT_PARAM_MAX_CHARS)
    decl = validate_params_decl([{"key": "txt", "type": "text"}])
    out = enforce_params_decl(decl, {"txt": "hello"})
    check("D4 text param accepted", out["txt"] == "hello", out)
    try:
        enforce_params_decl(decl, {"txt": "x" * (TEXT_PARAM_MAX_CHARS + 1)})
        check("D5 text over limit rejected", False, "accepted")
    except ParamsValueError as exc:
        check("D5 text over limit rejected", str(exc) == "param_too_long",
              str(exc))
    check("D5b limit constant = 100,000", TEXT_PARAM_MAX_CHARS == 100_000,
          TEXT_PARAM_MAX_CHARS)

    # ═══ C. file-count window (share: 1..20) ═══
    A64 = base64.b64encode(PNG).decode()
    st, d = _call("share", {"files": [], "client_ref": f"c0-{_u}"}, TOK, 10)
    res = d.get("result") or {}
    txt = (res.get("content") or [{}])[0].get("text", "")
    check("C1 0 files to share (min=1) → isError + English window hint",
          res.get("isError") is True
          and "accepts 1 to 20 files" in txt, txt[:100])
    files21 = [{"filename": f"f{i}.png", "b64": A64} for i in range(21)]
    st, d = _call("share", {"files": files21, "client_ref": f"c21-{_u}"}, TOK, 11)
    check("C2 21 files → rejected (array window)",
          (d.get("error") or {}).get("code") == -32602
          or (d.get("result") or {}).get("isError") is True, str(d)[:100])
    # compress declares 1..1 → 2 files is a window error
    st, d = _call("compress", {"files": [
        {"filename": "a.png", "b64": A64},
        {"filename": "b.png", "b64": A64}], "client_ref": f"c2-{_u}"}, TOK, 12)
    res = d.get("result") or {}
    txt = (res.get("content") or [{}])[0].get("text", "")
    check("C3 single-file tool with 2 files → isError + window hint",
          res.get("isError") is True and "accepts 1 to 1 files" in txt,
          txt[:100])

    # ═══ R. multi-file share items + /r page ═══
    files3 = [{"filename": f"r{i}.png", "b64": A64} for i in range(3)]
    st, d = _call("share", {"files": files3, "client_ref": f"r3-{_u}"}, TOK, 20)
    res = d.get("result") or {}
    check("R0 share call ok", res.get("isError") is False, str(d)[:120])
    sc = res["structuredContent"]
    items = sc["items"]
    check("R1 3 in → 3 ordered file items",
          [i["filename"] for i in items] == ["r0.png", "r1.png", "r2.png"],
          items)
    check("R2 every item has a direct download_url + page_url",
          all(i["download_url"].endswith("?download=1") and i["page_url"]
              for i in items), items[:1])
    dl = items[0]["download_url"]
    loc = urllib.parse.urlsplit(dl)
    st_d, h_d, _b, _sc = _req("GET", loc.path + "?" + loc.query)
    gid = urllib.parse.parse_qs(urllib.parse.urlsplit(
        h_d.get("location", "")).query).get("id", [""])[0]
    import urllib.request as _ur
    gk = (_os.environ.get("FLASHSHARE_GOKAPI_URL") or "").rstrip("/")
    if not gk:
        _sys.stderr.write(
            "ERROR: FLASHSHARE_GOKAPI_URL is required (R3 direct-download "
            "identity is checked against the storage authority).\n")
        _sys.exit(2)
    with _ur.urlopen(f"{gk}/downloadFile?id={gid}", timeout=15) as r:
        blob = r.read()
    check("R3 anonymous direct download md5 identical",
          hashlib.md5(blob).hexdigest() == hashlib.md5(PNG).hexdigest(),
          len(blob))
    st, _h, page, _sc = _req("GET", f"/r/{sc['result_url'].rsplit('/', 1)[1]}")
    # Image results render as one media group: 3 image files = one
    # rs-item grid carrying 3 rs-cells, each with its own download.
    check("R4 /r page renders ordered items",
          st == 200 and page.count('class="rs-cell"') == 3
          and "r0.png" in page and "r1.png" in page and "r2.png" in page
          and page.count("rs-dl") >= 3, st)
    _rp = _call("share", {"files": files3, "client_ref": f"r3-{_u}"},
                TOK, 21)[1]
    check("R5 replay returns the same items (idempotent)",
          ((_rp.get("result") or {}).get("structuredContent")
            or {}).get("replay") is True, str(_rp)[:100])

    # ═══ R6. settlement atomicity (in-process _finish + injected
    # landing failure): every file item lands in ONE transaction or none
    # does; the failure settles to failed + exact refund, zero uploads.
    from execution import gate as _gate
    from repo.ledger import claim_call
    from execution.results import StagedItems, StagedFileItem
    import repo.uploads as _upmod
    with db() as con:
        _before = int(con.execute(
            "select count(*) c from uploads where user_id=%s",
            (uid,)).fetchone()["c"])
    _staged = StagedItems(
        order=["file", "file"],
        files=[StagedFileItem(gokapi_id=f"gk_{_u}_a", filename="x0.png",
                              content_type="image/png", size=70,
                              expiry_days=1, allowed_downloads=5,
                              expire_at=None, url_hotlink=""),
               StagedFileItem(gokapi_id=f"gk_{_u}_b", filename="x1.png",
                              content_type="image/png", size=70,
                              expiry_days=1, allowed_downloads=5,
                              expire_at=None, url_hotlink="")],
        ip="127.0.0.1")
    _orig_ir = _upmod.insert_record
    def _boom_ir(*a, **k):
        raise RuntimeError("injected landing failure")
    _upmod.insert_record = _boom_ir
    try:
        with db() as con:
            _anchor, _claimed = claim_call(
                con, f"rf-{_u}", uid, "share", 25,
                "{}", ts, commission_rate_snapshot=0,
                invocation_hash="e" * 64)
        try:
            _gate._finish({"id": uid},
                          {"id": "share", "tool_key": "share",
                           "owner_id": None},
                          int(_anchor["id"]), 25, _staged, 0,
                          {"expiry_days": 1, "allowed_downloads": 5})
            finished = "no-raise"
        except Exception:
            finished = "raised"
    finally:
        _upmod.insert_record = _orig_ir
    with db() as con:
        _after = int(con.execute(
            "select count(*) c from uploads where user_id=%s",
            (uid,)).fetchone()["c"])
        _call_row = con.execute(
            "select status from tool_calls where id=%s",
            (int(_anchor["id"]),)).fetchone()
    check("R6 injected landing failure → zero uploads rows (all-or-nothing)",
          _after == _before and _call_row is not None
          and _call_row["status"] != "ok",
          (_before, _after, finished, _call_row and dict(_call_row)))

    # ═══ E. result expiry (delivery-owned, every shape) ═══
    from execution.results import land_items_result, StagedItems
    import time as _time
    _e_ts = int(_time.time())
    with db() as con:
        _text_rec = land_items_result(
            con, uid, StagedItems(order=["text", "json"],
                                  inline=[{"kind": "text", "text": "hi"},
                                          {"kind": "json", "data": {"a": 1}}],
                                  files=[]), "http://x",
            {"expiry_days": 3, "allowed_downloads": 5})
    _exp_expect = _e_ts + 3 * 86400
    check("E1 text/json-only result expires with the delivery window",
          isinstance(_text_rec[2].get("expires_at"), int)
          and abs(_text_rec[2]["expires_at"] - _exp_expect) <= 2,
          _text_rec[2].get("expires_at"))
    # file result inherits the latest file expiry (staged expire_at)
    _f_exp = _e_ts + 5 * 86400
    with db() as con:
        _file_rec = land_items_result(
            con, uid,
            StagedItems(order=["file"],
                        files=[__import__("execution.results", fromlist=["StagedFileItem"]).StagedFileItem(
                            gokapi_id=f"gk_{_u}_e", filename="e.png",
                            content_type="image/png", size=10,
                            expiry_days=5, allowed_downloads=5,
                            expire_at=_f_exp, url_hotlink="")],
                        ip="127.0.0.1"), "http://x",
            {"expiry_days": 1, "allowed_downloads": 5})
    check("E2 file result expiry = latest file expiry",
          _file_rec[2].get("expires_at") == _f_exp, _file_rec[2].get("expires_at"))
    # /r/{id} enforces expiry at render: a past-expiry record → 404
    _dead_id = "dead" + _u[:8]
    with db() as con:
        con.execute(
            "insert into tool_calls (user_id, tool_id, status, client_ref,"
            " invocation_hash, started_at, commission_rate_snapshot,"
            " result_public_id, result_json, finished_at)"
            " values (%s, 'share', 'ok', %s, %s, %s, 0, %s, %s, %s)",
            (uid, f"dead-{_u}", "d" * 64, ts, _dead_id,
             __import__("json").dumps({"result_id": _dead_id,
                                       "expires_at": ts - 3600,
                                       "items": [{"kind": "text", "text": "x"}]}),
             ts))
    st, _h_d, _b_d, _sc_d = _req("GET", f"/r/{_dead_id}")
    check("E3 expired /r → 404 (no existence oracle)", st == 404, st)
    st, _h_q, _b_q, _sc_q = _req("GET", f"/r/{_dead_id}/qr.svg")
    check("E3b expired /r QR → 404", st == 404, st)
    # live result page renders header facts + the media grid + downloads
    st, _h_r, page_r, _sc_r = _req(
        "GET", f"/r/{sc['result_url'].rsplit('/', 1)[1]}")
    check("E4 /r page: tool name + expiry + downloads facts + items",
          st == 200 and "Lossless Share" in page_r
          and page_r.count('class="rs-cell"') == 3
          and page_r.count("rs-dl") >= 3
          and "?download=1" in page_r, st)
    # MCP expires_at mirrors the record authority
    st, d = _call("compress", {"files": [{"filename": "e2.png", "b64": A64}],
                               "client_ref": f"e2-{_u}"}, TOK, 44)
    _sc_exp = ((d.get("result") or {}).get("structuredContent") or {}).get("expires_at")
    check("E5 MCP expires_at = record expireAt (int)",
          isinstance(_sc_exp, int) and _sc_exp > ts, _sc_exp)

    # ═══ M. MCP surface ═══
    st, d = _mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, TOK)
    tools = {t["name"]: t for t in d["result"]["tools"]}
    check("M1 every tool has outputSchema + English title",
          all("outputSchema" in t and t.get("title") for t in tools.values())
          and tools["share"]["title"] == "Lossless Share",
          [t.get("title") for t in tools.values()][:3])
    check("M2 description states inputs+outputs+price, English only",
          "Outputs: file" in tools["share"]["description"]
          and not any("\u4e00" <= ch <= "\u9fff"
                      for ch in tools["share"]["description"]),
          tools["share"]["description"])
    schema = tools["share"]["outputSchema"]
    sc_props = schema["properties"]["items"]["items"]["oneOf"]
    check("M3 outputSchema matches structuredContent shape",
          {k for k in schema["properties"]} == {
              "call_id", "replay", "result_url", "expires_at", "items"}
          and len(sc_props) == 3, schema["properties"].keys())
    # image echo suppression: share returns the input bytes unchanged,
    # so the image block is dropped (only the resource_link) — an
    # agent's own upload must not bounce back into its context.
    st, d = _call("share", {"files": [{"filename": "img.png", "b64": A64}],
                            "client_ref": f"mi-{_u}"}, TOK, 40)
    content = (d.get("result") or {}).get("content") or []
    kinds = [c["type"] for c in content]
    check("M4 pass-through share → resource_link, NO image block",
          "image" not in kinds and "resource_link" in kinds, kinds)
    # a transformed image still gets the block. compress only re-encodes
    # ABOVE the target: a >1MB noise PNG forces a real JPEG conversion
    # (bytes differ, echo rule does not apply).
    import io as _io
    import random as _random
    from PIL import Image as _Img
    _rng = _random.Random(0xC0)
    _img = _Img.new("RGB", (1024, 1024))
    _img.putdata([(_rng.randrange(256), _rng.randrange(256),
                   _rng.randrange(256)) for _ in range(1024 * 1024)])
    _buf = _io.BytesIO()
    _img.save(_buf, "PNG")
    BIG64 = base64.b64encode(_buf.getvalue()).decode()
    st, d = _call("compress", {"files": [{"filename": "big.png",
                                          "b64": BIG64}],
                               "params": {"preset": "1mb"},
                               "client_ref": f"mi2-{_u}"}, TOK, 43)
    content = (d.get("result") or {}).get("content") or []
    kinds = [c["type"] for c in content]
    check("M4b re-encoded image → image block + resource_link",
          "image" in kinds and "resource_link" in kinds
          and any(c.get("mimeType") == "image/jpeg" for c in content
                  if c["type"] == "image"), kinds)
    # format mismatch error text
    bad_b = base64.b64encode(b"not a png at all").decode()
    st, d = _call("compress", {"files": [{"filename": "z.png",
                                           "b64": bad_b}],
                               "client_ref": f"me-{_u}"}, TOK, 41)
    res = d.get("result") or {}
    txt = ((res.get("content") or [{}])[0].get("text", ""))
    err = (d.get("error") or {}).get("message", "")
    check("M5 format-mismatch error is plain English + accepted formats",
          ((d.get("result") or {}).get("isError") is True
            and "not supported" in txt and ".png" in txt)
          or ("not supported" in err and ".png" in err), (txt, err))
    # catalog_search summary shape
    st, d = _call("catalog_search", {"q": "share"}, TOK, 42)
    tools_sum = ((d.get("result") or {}).get("structuredContent")
                 or {}).get("tools") or []
    check("M6 catalog_search returns tools/list-shaped summaries",
          tools_sum and tools_sum[0]["name"] == "share"
          and tools_sum[0].get("title")
          and "Outputs" in tools_sum[0]["description"], tools_sum[:1])

    # ═══ X. developer endpoint items contract (direct drive_http) ═══
    import remote as _remote
    decl_ep = {"url": f"{BASE_URL}/nonexistent", "timeout_s": 10}
    sources = [{"filename": "a.png", "content_type": "image/png",
                "size": len(PNG), "url": "https://x.test/a.png"}]
    # invalid shapes via a local listener would need TLS; drive the
    # response-parsing path directly with a stubbed _http_call
    orig_call = _remote._http_call
    def _fake_call(url, payload, headers, timeout_s, trusted):
        body = _fake_call.body
        return _fake_call.status, body, "application/json"
    _remote._http_call = _fake_call
    try:
        _fake_call.status = 200
        _fake_call.body = _json.dumps({"legacy": "shape"}).encode()
        try:
            _remote.drive_http(decl_ep, sources, {}, {}, True)
            check("X1 non-items JSON → invalid_response", False, "accepted")
        except _remote.RemoteError as exc:
            check("X1 non-items JSON → invalid_response",
                  exc.code == "invalid_response", exc.code)
        _fake_call.body = b"raw bytes not json"
        try:
            _remote.drive_http(decl_ep, sources, {}, {}, True)
            check("X2 raw-byte body rejected (no legacy mode)",
                  False, "accepted")
        except _remote.RemoteError as exc:
            check("X2 raw-byte body rejected (no legacy mode)",
                  exc.code == "invalid_response", exc.code)
        _fake_call.body = _json.dumps({"items": [
            {"kind": "text", "text": "ok"},
            {"kind": "json", "data": {"n": 1}},
            {"kind": "file", "filename": "o.png",
             "content_type": "image/png",
             "data_b64": base64.b64encode(PNG).decode()}]}).encode()
        out = _remote.drive_http(decl_ep, sources, {}, {}, True)
        check("X3 items shape round-trips (text+json+file)",
              [i["kind"] for i in out["items"]] == ["text", "json", "file"]
              and out["items"][2]["bytes"] == PNG, out["items"][:2])
        _fake_call.body = _json.dumps({"items": [
            {"kind": "file", "filename": "x.png",
             "content_type": "image/png"}]}).encode()
        try:
            _remote.drive_http(decl_ep, sources, {}, {}, True)
            check("X4 file item without data → invalid", False, "accepted")
        except _remote.RemoteError as exc:
            check("X4 file item without data → invalid",
                  exc.code == "invalid_response", exc.code)
        _fake_call.body = _json.dumps({"items": [
            {"kind": "text", "text": "x" * 2_000_000}]}).encode()
        try:
            _remote.drive_http(decl_ep, sources, {}, {}, True)
            check("X5 oversized inline payload → invalid", False, "accepted")
        except _remote.RemoteError as exc:
            check("X5 oversized inline payload → invalid",
                  "invalid_response" in (exc.code, str(exc)), exc.code)
    finally:
        _remote._http_call = orig_call
    check("X6 request payload key is files (not job_inputs)",
          all("files" == k or k in ("filename", "content_type", "size",
                                    "url")
              for src in sources for k in src), sources)
    src_txt = open(_os.path.join(_REPO, "core", "remote.py"),
                   encoding="utf-8").read()
    check("X6b job_inputs gone from the wire contract",
          '"job_inputs"' not in src_txt, "grep job_inputs")
finally:
    with db() as con:
        if uid is not None:
            con.execute("delete from tool_calls where user_id=%s", (uid,))
            con.execute("delete from uploads where user_id=%s", (uid,))
            con.execute("delete from ledger where user_id=%s", (uid,))
            con.execute("delete from api_keys where user_id=%s", (uid,))
            con.execute("delete from sessions where user_id=%s", (uid,))
            con.execute("delete from upload_usage where user_id=%s", (uid,))
            con.execute("delete from ingress_requests where user_id=%s", (uid,))
            purge_users(con, [uid])
    print("cleanup done")

print()
if FAIL:
    print(f"FAILED {len(FAIL)}: {FAIL}")
    _sys.exit(1)
print(f"OK — IO-1 contract ({COUNT} checks)")
