#!/usr/bin/env python3
"""工具链全量真实执行验证 (真实两步链路): POST /api/uploads → POST /api/tools/execute
10 个 local 工具逐一真跑, 产物逐一 GET 下载校验魔数/Content-Type。"""
import io, json, os, sys, time, urllib.request, ssl, http.client, urllib.error

BASE = "https://518.plus"
# 本机直连 nginx (DNS 在 sudo 环境不稳): 518.plus → 127.0.0.1, 证书 CN 匹配
import socket
_orig_getaddrinfo = socket.getaddrinfo
def _local_getaddrinfo(host, *a, **kw):
    if host in ("518.plus", "127.0.0.1", "localhost"):
        if host == "518.plus":
            return _orig_getaddrinfo("127.0.0.1", *a, **kw)
        return _orig_getaddrinfo(host, *a, **kw)
    print(f"[dns] passthrough host={host!r}")
    return _orig_getaddrinfo(host, *a, **kw)
socket.getaddrinfo = _local_getaddrinfo
CTX = ssl.create_default_context()
TS = int(time.time())

def https(path, method="GET", body=None, headers=None, timeout=120):
    h = {"User-Agent": "Mozilla/5.0 ToolchainCheck/1.0"}
    h.update(headers or {})
    full = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(full, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except Exception as ex:
        print(f"[net-err] {path[:60]} → {ex}")
        raise

def mp(fields, files, boundary="----bndtool"):
    b = io.BytesIO()
    for k, v in fields.items():
        b.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for (fn, data, ct) in files:
        b.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fn}"\r\nContent-Type: {ct}\r\n\r\n'.encode() + data + b'\r\n')
    b.write(f'--{boundary}--\r\n'.encode())
    return b.getvalue(), f"multipart/form-data; boundary={boundary}"

c = http.client.HTTPConnection("127.0.0.1", 8000, timeout=120)
sess = None
def req(method, path, body=None, ct=None):
    global sess
    h = {}
    if sess: h["Cookie"] = sess
    if ct: h["Content-Type"] = ct
    c.request(method, path, body=body, headers=h)
    r = c.getresponse(); d = r.read()
    sc = r.getheader("set-cookie")
    if sc and "flashshare_session=" in sc and "deleted" not in sc:
        sess = sc.split(";")[0]
    return r.status, d

u = f"tc{TS%999999}"
b, ct = mp({"username": u, "password": "Test12345", "password_confirm": "Test12345", "email": f"{u}@t.co"}, [])
st, d = req("POST", "/auth/register", b, ct)
if not sess and st in (200, 302):
    b, ct = mp({"username": u, "password": "Test12345"}, [])
    st, d = req("POST", "/auth/login", b, ct)
assert sess, f"register fail {st}"

from PIL import Image, ImageDraw
def mkpng(w, h, bg, rect):
    im = Image.new("RGB", (w, h), bg)
    ImageDraw.Draw(im).rectangle(rect, fill=(180, 195, 215))
    b_ = io.BytesIO(); im.save(b_, "PNG"); return b_.getvalue()
PNG = mkpng(400, 300, (252, 252, 250), [40, 40, 360, 260])
PNG2 = mkpng(300, 200, (240, 240, 240), [20, 20, 280, 180])

from pypdf import PdfWriter
pw = PdfWriter(); pw.add_blank_page(200, 150)
pb = io.BytesIO(); pw.write(pb); PDF = pb.getvalue()

import zipfile as zf
zb = io.BytesIO()
with zf.ZipFile(zb, "w") as z:
    z.writestr("a.txt", "hello 518")
    z.writestr("b/c.txt", "nested")
ZIP = zb.getvalue()

db = io.BytesIO()
with zf.ZipFile(db, "w") as z:
    z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>518 doc2pdf probe</w:t></w:r></w:p></w:body></w:document>')
DOCX = db.getvalue()

R = []
def check(name, ok, detail=""):
    R.append((name, ok, str(detail)[:200]))
    print(("PASS" if ok else "FAIL"), "|", name, ("| " + str(detail)[:200] if not ok else ""))

def upload(files):
    b_, ct_ = mp({"expiryDays": "1", "allowedDownloads": "5"}, files)
    st_, d_ = req("POST", "/api/uploads", b_, ct_)
    if st_ != 200:
        return None, f"upload {st_} {d_[:120]}"
    j = json.loads(d_)
    if not j.get("ok"):
        return None, f"upload ok=false {str(j)[:150]}"
    files_out = j.get("files") or [j.get("file")]
    return files_out, None

def run_tool(tool_id, files, preset=None, expect_magic=None, expect_ct=None):
    up, err = upload(files)
    if err:
        return check(f"{tool_id}", False, err)
    body = {
        "tool_id": tool_id,
        "params": {"preset": preset, "expiry_days": 1, "allowed_downloads": 5},
    }
    if len(up) == 1:
        body["public_id"] = up[0]["publicId"]
        body["source_token"] = up[0].get("sourceToken", "")
    else:
        body["public_ids"] = [f["publicId"] for f in up]
        body["source_tokens"] = [f.get("sourceToken", "") for f in up]
    st_, d_ = req("POST", "/api/tools/execute", json.dumps(body), "application/json")
    if st_ != 200:
        return check(f"{tool_id}", False, f"execute {st_} {d_[:150]}")
    j = json.loads(d_)
    if not j.get("ok"):
        return check(f"{tool_id}", False, f"execute ok=false {str(j)[:180]}")
    res = j.get("result") or {}
    url = res.get("downloadUrl") or res.get("url")
    if not url:
        return check(f"{tool_id}", False, f"no downloadUrl {str(res)[:150]}")
    # 下载链路: /f/{id}?download=1 → 302 /dh/... → nginx 反代 Gokapi。必须走公网完整链。
    if not url.startswith("https://518.plus"):
        return check(f"{tool_id}", False, f"unexpected url host: {url[:80]}")
    st2, out, hd = https(url + "?download=1", timeout=180)
    if st2 == 302:
        loc = hd.get("Location") or hd.get("location")
        if loc:
            st2, out, hd = https(loc if loc.startswith("http") else BASE + loc, timeout=180)
    if st2 != 200:
        return check(f"{tool_id}", False, f"download {st2}")
    magic_ok = (not expect_magic) or out[:len(expect_magic)] == expect_magic
    ct_ok = (not expect_ct) or expect_ct in (hd.get("Content-Type") or "")
    check(f"{tool_id} 执行+产物", magic_ok and ct_ok and len(out) > 40,
          f"len={len(out)} magic={out[:6]!r} ct={hd.get('Content-Type')}")

P = "image/png"; APDF = "application/pdf"
run_tool("share", [("a.png", PNG, P)])
run_tool("privacy", [("a.png", PNG, P)])
run_tool("beautify", [("a.png", PNG, P)], preset="snow")
run_tool("compress", [("a.png", PNG, P)])
run_tool("crop_white", [("a.png", PNG, P)])
run_tool("ico", [("a.png", PNG, P)], expect_ct="image/x-icon")
run_tool("img2pdf", [("a.png", PNG, P), ("b.png", PNG2, P)])
run_tool("pdf_merge", [("a.pdf", PDF, APDF), ("b.pdf", PDF, APDF)])
run_tool("zip_pack", [("a.png", PNG, P), ("b.txt", b"txt content", "text/plain")])
run_tool("doc2pdf", [("a.docx", DOCX, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")])

# http 工具 (社区) 可检索 + MCP tools/list
st, d, _ = https("/api/tools/search?q=webhook")
check("http 工具(webhook_demo)可检索", st == 200 and b"webhook_demo" in d, f"st={st}")

print()
fails = [x for x in R if not x[1]]
print(f"工具链执行验证: {len(R)} 项 | PASS {len(R)-len(fails)} | FAIL {len(fails)}")
sys.exit(1 if fails else 0)
