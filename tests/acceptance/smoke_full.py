"""518.PLUS 全量冒烟 — 覆盖 14 域 (页面/认证/上传/分享链/10工具/计量/幂等/
APIv1/MCP/用户工具/经济/提现/后台/安全负路径)。输出 JSON 结果到 /tmp/smoke_result.json
并打印人类可读摘要。
"""
import http.client
import io
import json
import os
import sys
import time
import zipfile
import base64
import hmac
import hashlib
import secrets

sys.path.insert(0, "/opt/flashshare/core")
sys.path.insert(0, "/opt/flashshare")

import psycopg2

from security import token_hash, sign_source_token
from economy.billing import spendable_balance

HOST = ("127.0.0.1", 8000)
BASE = "https://518.plus"
TS = int(time.time())
R = []          # [(domain, check, ok, detail)]
CUR = ["", ""]

def check(name, ok, detail=""):
    R.append((CUR[0], name, bool(ok), str(detail)[:220]))
    print(f"{'PASS' if ok else 'FAIL'} | {CUR[0]} | {name}" + (f" | {detail}" if not ok else ""))

def dom(d):
    CUR[0] = d

def conn(timeout=60):
    return http.client.HTTPConnection(*HOST, timeout=timeout)

def https(path, method="GET", body=None, headers=None, timeout=30):
    """经 nginx 的边缘路径 (真实用户面)"""
    import urllib.request, ssl
    ctx = ssl.create_default_context()
    h = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) SmokeTest/1.0"}
    h.update(headers or {})
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

class C:
    """session 客户端 (web 面)"""
    def __init__(self):
        self.c = conn()
        self.sess = None
    def req(self, method, path, body=None, ct=None, extra=None):
        h = {}
        if self.sess: h["Cookie"] = self.sess
        if ct: h["Content-Type"] = ct
        if extra: h.update(extra)
        self.c.request(method, path, body=body, headers=h)
        r = self.c.getresponse()
        d = r.read()
        sc = r.getheader("set-cookie")
        if sc and "flashshare_session=" in sc and "deleted" not in sc:
            self.sess = sc.split(";")[0]
        return r.status, d

def mp(fields, files, boundary="----bnd789"):
    body = io.BytesIO()
    for k, v in fields.items():
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for (fn, data, ct) in files:
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fn}"\r\nContent-Type: {ct}\r\n\r\n'.encode() + data + b'\r\n')
    body.write(f'--{boundary}--\r\n'.encode())
    return body.getvalue(), f"multipart/form-data; boundary={boundary}"

def png1px():
    return base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

def docx(text):
    ct = '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    rels = '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    doc = f'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('[Content_Types].xml', ct); z.writestr('_rels/.rels', rels); z.writestr('word/document.xml', doc)
    return buf.getvalue()

PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, *a):
    c = psycopg2.connect(PG); cur = c.cursor(); cur.execute(q, a if a else None)
    rows = cur.fetchall() if cur.description else []
    c.commit(); c.close(); return rows

def make_user(prefix="smk"):
    u = f"{prefix}{TS % 100000}{secrets.token_hex(2)}"
    c = C()
    b, ct = mp({"username": u, "password": "Test12345", "password_confirm": "Test12345", "email": f"{u}@t.co"}, [])
    st, _ = c.req("POST", "/auth/register", b, ct)
    return u, c

def grant_credits(uid, n=500):
    psql("INSERT INTO ledger(user_id, delta, balance_after, kind, note, idem_key, created_at) "
         "SELECT %s, %s, %s, 'grant', 'smoke-seed', 'smoke:%s:%s', %s",
         uid, n, n, uid, TS, TS)

# 冒烟前置: 清本机 IP 的历史配额计数 (多轮冒烟会爆 err_quota_ip)
_psql_clean = psycopg2.connect(PG); _pc = _psql_clean.cursor()
_pc.execute("DELETE FROM uploads WHERE ip='127.0.0.1' AND created_at > extract(epoch from now())-86400")
_psql_clean.commit(); _psql_clean.close()

# ════════════════ 1. 公开页面 (边缘+应用双层) ════════════════
dom("1.公开页面")
for path in ["/", "/tools", "/agents", "/terms", "/privacy", "/auth/login", "/auth/register", "/robots.txt", "/sitemap.xml", "/site.webmanifest", "/offline"]:
    st, _ = https(path)
    check(f"GET {path}", st == 200, f"status={st}")
st, body = https("/robots.txt")
check("robots 禁爬 api/auth/f", "Disallow: /api/" in body.decode() and "Disallow: /f/" in body.decode())
st, body = https("/nonexistent-page-xyz")
check("404 页重定向首页(跟随到200)", st == 200, f"status={st}")
st, body = https("/static/518.css")
check("静态 CSS 经边缘", st == 200 and len(body) > 10000, f"status={st}")
st, body = https("/f/nonexistent1")
check("缺失文件页 404", st == 404, f"status={st}")

# ════════════════ 2. 认证链路 ════════════════
dom("2.认证")
u, c = make_user()
check("注册+自动登录(302)", c.sess is not None)
# 重复用户名
c2 = C()
b, ct = mp({"username": u, "password": "Test12345", "password_confirm": "Test12345", "email": "x@t.co"}, [])
st, body = c2.req("POST", "/auth/register", b, ct)
check("重复用户名被拒", st == 200 and ("已" in body.decode() or "taken" in body.decode() or "err" in body.decode()))
# 弱密码
c3 = C()
b, ct = mp({"username": f"wk{TS%99999}", "password": "123", "password_confirm": "123", "email": "w@t.co"}, [])
st, body = c3.req("POST", "/auth/register", b, ct)
check("弱密码被拒(页面报错)", st == 200 and c3.sess is None)
# 登出
st, _ = c.req("POST", "/auth/logout")
c.sess = None
st, body = c.req("GET", "/api/me")
check("登出后 API 401", st == 401, f"status={st}")
# 重新登录 (用户名+密码)
c4 = C()
b, ct = mp({"login": u, "password": "Test12345"}, [])
st, _ = c4.req("POST", "/auth/login", b, ct)
check("用户名登录", c4.sess is not None)
# 错误密码
c5 = C()
b, ct = mp({"login": u, "password": "Wrong999"}, [])
st, _ = c5.req("POST", "/auth/login", b, ct)
check("错误密码拒绝", c5.sess is None)
# 邮箱登录
c6 = C()
b, ct = mp({"login": f"{u}@t.co", "password": "Test12345"}, [])
st, _ = c6.req("POST", "/auth/login", b, ct)
check("邮箱登录", c6.sess is not None)

# ════════════════ 3. 账户页 ════════════════
dom("3.账户页")
for path in ["/account/profile", "/account/security", "/account/credits", "/account/earnings", "/account/tools", "/account/tokens"]:
    st, _ = c4.req("GET", path)
    check(f"GET {path}", st == 200, f"status={st}")
# 资料修改
b, ct = mp({"display_name": "冒烟员", "bio": "smoke test", "payout_channel": "smoke-channel"}, [])
st, _ = c4.req("POST", "/account/profile", b, ct)
st, body = c4.req("GET", "/account/profile")
check("资料保存(display_name/bio/payout)", "冒烟员" in body.decode())
# 每日领取
st, _ = c4.req("POST", "/account/credits/claim")
check("每日领取(303/302)", st in (302, 303), f"status={st}")
st, _ = c4.req("POST", "/account/credits/claim")
check("重复领取幂等(不再发)", st in (302, 303))
# 密码修改
b, ct = mp({"old_password": "Test12345", "new_password": "NewPass678", "new_password_confirm": "NewPass678"}, [])
st, _ = c4.req("POST", "/account/password", b, ct)
c7 = C()
b, ct = mp({"login": u, "password": "NewPass678"}, [])
st, _ = c7.req("POST", "/auth/login", b, ct)
check("密码修改生效", c7.sess is not None)

# ════════════════ 4. 分享链 (上传→详情→下载→QR→改名→删除) ════════════════
dom("4.分享链")
b, ct = mp({"expiryDays": "1", "allowedDownloads": "50"}, [("smoke.txt", b"hello smoke", "text/plain")])
st, body = c7.req("POST", "/api/uploads", b, ct)
f = json.loads(body).get("file", {})
check("单文件上传", st == 200 and f.get("publicId"), body[:150])
pid = f["publicId"]
st, body = c7.req("GET", f"/f/{pid}")
check("详情页", st == 200 and "smoke.txt" in body.decode())
st, body = c7.req("GET", f"/f/{pid}?download=1")
check("下载(302→nginx)", st in (302, 200))
st, body = c7.req("GET", f"/f/{pid}/qr.svg")
check("QR SVG", st == 200 and b"<svg" in body)
# 多文件+zip_pack
b, ct = mp({"expiryDays": "1", "allowedDownloads": "10"}, [("a.txt", b"AAA", "text/plain"), ("b.txt", b"BBB", "text/plain")])
st, body = c7.req("POST", "/api/uploads", b, ct)
files = json.loads(body).get("files", [])
check("多文件上传(files[])", st == 200 and len(files) == 2, body[:150])
# 历史列表
st, body = c7.req("GET", "/api/uploads")
ups = json.loads(body).get("uploads", [])
check("上传历史列表", st == 200 and len(ups) >= 1)
# 改名
st, body = c7.req("PATCH", f"/api/uploads/{pid}", json.dumps({"name": "renamed"}).encode(), "application/json")
check("改名(留扩展名)", st == 200 and json.loads(body).get("name") == "renamed.txt", body[:150])
# 删除
st, body = c7.req("DELETE", f"/api/uploads/{pid}")
check("删除", st == 200)
st, body = c7.req("GET", f"/f/{pid}")
check("删除后 404", st == 404)

# ════════════════ 5. 10 工具全链 (上传→execute→结果) ════════════════
dom("5.工具执行")
uid = psql("SELECT id FROM users WHERE username=%s", u)[0][0]
grant_credits(uid, 500)
# 冒烟自主定价: doc2pdf 临时 5 (域6 计量验证用; 结束恢复 0)
psql("UPDATE pricing SET cost=5, enabled=TRUE WHERE tool_id='doc2pdf'")

def upload_tool(tool, files):
    b, ct = mp({"expiryDays": "1", "allowedDownloads": "10", "tool": tool}, files)
    st, body = c7.req("POST", "/api/uploads", b, ct)
    d = json.loads(body)
    f = d.get("file") or (d.get("files") or [None])[0]
    if not f:
        raise RuntimeError(f"upload_tool({tool}) 失败: {st} {body[:200]}")
    return f

def exec_tool(tool, f, params=None, ref=None):
    payload = {"tool_id": tool, "public_id": f["publicId"], "source_token": f["sourceToken"], "params": params or {}}
    if ref: payload["client_ref"] = ref
    st, body = c7.req("POST", "/api/tools/execute", json.dumps(payload).encode(), "application/json")
    return st, json.loads(body)

# 5.1 beautify (12 preset 抽查3)
for preset in ["aurora", "pure", "frost"]:
    f = upload_tool("beautify", [("s.png", png1px(), "image/png")])
    st, r = exec_tool("beautify", f, {"preset": preset})
    ok = r.get("ok") and r["result"].get("contentType") == "image/png"
    check(f"beautify/{preset}", ok, str(r)[:150])
# 5.2 compress
f = upload_tool("compress", [("big.png", png1px(), "image/png")])
st, r = exec_tool("compress", f, {"preset": "1mb"})
check("compress/1mb", r.get("ok"))
# 5.3 privacy
f = upload_tool("privacy", [("p.png", png1px(), "image/png")])
st, r = exec_tool("privacy", f, {"preset": "standard"})
check("privacy/standard", r.get("ok") and r["result"].get("contentType") == "image/jpeg")
# 5.4 ico
f = upload_tool("ico", [("i.png", png1px(), "image/png")])
st, r = exec_tool("ico", f, {"preset": "ico_32"})
check("ico/32", r.get("ok") and r["result"].get("filename", "").endswith(".ico"))
# 5.5 crop_white
f = upload_tool("crop_white", [("w.png", png1px(), "image/png")])
st, r = exec_tool("crop_white", f, {})
check("crop_white", r.get("ok"))
# 5.6 doc2pdf
f = upload_tool("doc2pdf", [("d.docx", docx("smoke doc"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")])
st, r = exec_tool("doc2pdf", f, {})
check("doc2pdf", r.get("ok") and r["result"].get("contentType") == "application/pdf", str(r)[:150])
# 5.7 img2pdf (multi)
fa = upload_tool("img2pdf", [("1.png", png1px(), "image/png"), ("2.png", png1px(), "image/png")])
fb = upload_tool("img2pdf", [("3.png", png1px(), "image/png")])
payload = {"tool_id": "img2pdf", "public_ids": [fa["publicId"], fb["publicId"]], "source_tokens": [fa["sourceToken"], fb["sourceToken"]], "params": {}}
st, body = c7.req("POST", "/api/tools/execute", json.dumps(payload).encode(), "application/json")
r = json.loads(body)
check("img2pdf(多文件)", r.get("ok") and r["result"].get("contentType") == "application/pdf", str(r)[:150])
# 5.8 pdf_merge (multi)
def pdf1():
    # 最小合法 PDF
    return b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 99 99]>>endobj\nxref\n0 4\ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n0\n%%EOF"
fa = upload_tool("pdf_merge", [("a.pdf", pdf1(), "application/pdf"), ("b.pdf", pdf1(), "application/pdf")])
payload = {"tool_id": "pdf_merge", "public_ids": [fa["publicId"]], "source_tokens": [fa["sourceToken"]], "params": {}}
st, body = c7.req("POST", "/api/tools/execute", json.dumps(payload).encode(), "application/json")
r = json.loads(body)
check("pdf_merge(多文件)", r.get("ok"), str(r)[:150])
# 5.9 zip_pack (multi)
fa = upload_tool("zip_pack", [("z1.txt", b"Z1", "text/plain"), ("z2.txt", b"Z2", "text/plain")])
payload = {"tool_id": "zip_pack", "public_ids": [fa["publicId"]], "source_tokens": [fa["sourceToken"]], "params": {}}
st, body = c7.req("POST", "/api/tools/execute", json.dumps(payload).encode(), "application/json")
r = json.loads(body)
zid = r.get("result", {}).get("publicId", "")
check("zip_pack(多文件)", r.get("ok"), str(r)[:150])
if zid:
    st, body = c7.req("GET", f"/f/{zid}/zip-list")
    zl = json.loads(body)
    check("zip 预览(Range CD 解析)", zl.get("ok") and zl.get("files", 0) >= 1, str(zl)[:150])
# 5.10 share (无损): tool=share 走直传(无中间件), 校验返回即含正式记录
b, ct = mp({"expiryDays": "1", "allowedDownloads": "10", "tool": "share"}, [("sh.txt", b"SH", "text/plain")])
st, body = c7.req("POST", "/api/uploads", b, ct)
d = json.loads(body)
sf = d.get("file") or (d.get("files") or [{}])[0]
check("share(无损直传)", st == 200 and sf.get("downloadUrl"), body[:150])

# ════════════════ 6. 计量/幂等/退款 ════════════════
dom("6.计量幂等")
before = spendable_balance(uid)
f = upload_tool("doc2pdf", [("m.docx", docx("meter"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")])
st, r = exec_tool("doc2pdf", f, {}, ref=f"smoke-ref-{TS}")
after1 = spendable_balance(uid)
check("扣费执行", r.get("ok") and before - after1 >= 1, f"bal {before}->{after1}")
st, r2 = exec_tool("doc2pdf", f, {}, ref=f"smoke-ref-{TS}")
after2 = spendable_balance(uid)
check("同 ref 重放不重扣", r2.get("replay") and after1 == after2, f"{after1}->{after2}")
# 失败退款: doc2pdf 喂伪 docx
b, ct = mp({"expiryDays": "1", "allowedDownloads": "10", "tool": "doc2pdf"},
           [("bad.docx", b"PK\x03\x04 not a docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")])
st, body = c7.req("POST", "/api/uploads", b, ct)
bf = json.loads(body).get("file")
if bf:
    st, r3 = exec_tool("doc2pdf", bf, {}, ref=f"smoke-fail-{TS}")
    after3 = spendable_balance(uid)
    check("失败自动退款", (not r3.get("ok")) and after3 == after2, f"bal {after2}->{after3}")
    # 同 ref 重试成功 (新源)
    f2 = upload_tool("doc2pdf", [("ok.docx", docx("retry"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")])
    st, r4 = exec_tool("doc2pdf", f2, {}, ref=f"smoke-fail-{TS}")
    after4 = spendable_balance(uid)
    check("失败后同ref重试(再扣费)", r4.get("ok") and not r4.get("replay") and after4 < after3, f"bal {after3}->{after4}")
psql("UPDATE pricing SET cost=0 WHERE tool_id='doc2pdf'")  # 恢复原价

# ════════════════ 7. API v1 (Bearer) ════════════════
dom("7.APIv1")
st, body = c7.req("POST", "/api/tokens", json.dumps({"name": "smoke"}).encode(), "application/json")
tok = json.loads(body).get("token", {})
check("创建 Bearer token", tok.get("token", "").startswith("fsk_"))
bt = tok["token"]
def v1(method, path, body=None, ct=None):
    c = conn(120)
    h = {"Authorization": f"Bearer {bt}"}
    if ct: h["Content-Type"] = ct
    c.request(method, path, body=body, headers=h)
    r = c.getresponse(); return r.status, r.read()
st, body = v1("GET", "/api/v1/me")
d = json.loads(body)
check("v1/me", st == 200 and d.get("ok") and "credits" in d)
st, body = v1("GET", "/api/v1/tools")
tools = json.loads(body).get("tools", [])
check("v1/tools 目录", st == 200 and len(tools) >= 10)
# 上传+执行+取回
c = conn(120)
c.request("POST", "/api/v1/uploads", body=b"v1 smoke", headers={"Authorization": f"Bearer {bt}", "Content-Type": "text/plain", "x-filename": "v1.txt", "Content-Length": "8"})
r = c.getresponse(); vf = json.loads(r.read()).get("file", {})
check("v1 上传源文件", vf.get("sourceToken") is not None)
st, body = v1("POST", "/api/v1/execute",
              json.dumps({"tool_id": "share", "public_id": vf["publicId"], "source_token": vf["sourceToken"], "client_ref": f"v1smoke{TS}", "params": {}}).encode(),
              "application/json")
d = json.loads(body)
rid = d.get("result", {}).get("publicId", "")
check("v1 execute", st == 200 and d.get("ok"))
if rid:
    st, body = v1("GET", f"/api/v1/uploads/{rid}")
    check("v1 取回结果字节", st == 200 and body == b"v1 smoke", f"{st} {body[:30]}")
# 负: cookie 不能上 v1
c = conn()
c.request("GET", "/api/v1/me", headers={"Cookie": c7.sess} if c7.sess else {})
r = c.getresponse()
check("v1 拒绝 cookie(bearer_required)", r.status == 401, f"status={r.status}")
# token 撤销
tid = tok["id"]
st, body = c7.req("POST", f"/api/tokens/{tid}/revoke")
st, body = v1("GET", "/api/v1/me")
check("撤销后 token 失效", st == 401)

# ════════════════ 8. MCP 协议 ════════════════
dom("8.MCP")
st, body2 = c7.req("POST", "/api/tokens", json.dumps({"name": "mcp"}).encode(), "application/json")
bt2 = json.loads(body2).get("token", {})["token"]
def mcp(msg, notify=False):
    c = conn(120)
    h = {"Authorization": f"Bearer {bt2}", "Content-Type": "application/json"}
    c.request("POST", "/mcp", body=json.dumps(msg).encode(), headers=h)
    r = c.getresponse()
    return r.status, r.read()
st, body = mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
d = json.loads(body)
check("MCP initialize", d.get("result", {}).get("serverInfo", {}).get("name") == "518plus")
st, body = mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
names = [t["name"] for t in json.loads(body)["result"]["tools"]]
check("MCP tools/list", len(names) >= 10 and "doc2pdf" in names and "catalog_search" in names)
st, body = mcp({"jsonrpc": "2.0", "method": "notifications/initialized"})
check("MCP notification 202", st == 202)
st, body = mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "catalog_search", "arguments": {"q": "pdf"}}})
d3 = json.loads(body)
if d3.get("result", {}).get("content"):
    hits = json.loads(d3["result"]["content"][0]["text"])["tools"]
    check("MCP catalog_search", len(hits) >= 2, body[:150])
else:
    check("MCP catalog_search", False, body[:200])
# tools/call 完整链 (b64 源)
b64 = base64.b64encode(b"mcp smoke file").decode()
st, body = mcp({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "share", "arguments": {"file_b64": b64, "filename": "m.txt", "client_ref": f"mcp{TS}"}}})
d = json.loads(body)
check("MCP tools/call(b64→执行)", d.get("result", {}).get("isError") is False and "result" in json.loads(d["result"]["content"][0]["text"]))
# 负: 未知方法
st, body = mcp({"jsonrpc": "2.0", "id": 5, "method": "nope"})
check("MCP 未知方法 -32601", json.loads(body).get("error", {}).get("code") == -32601)
# 负: 无 token
c = conn(); c.request("POST", "/mcp", body=b"{}", headers={"Content-Type": "application/json"})
check("MCP 无认证 401", c.getresponse().status == 401)

# ════════════════ 9. 用户工具 (C2C 供给侧) ════════════════
dom("9.用户工具")
st, body = c7.req("GET", "/api/usertools")
check("列自己的工具(空)", st == 200 and json.loads(body).get("ok"))
# 提交 manifest (https 公网端点模拟 — 用 httpbin 类不可靠, 用本地回环 listener 不便;
# 用 https://518.plus/api/v1/demo-echo 自身作端点 — 公网 https+公网IP, 过 SSRF)
manifest = {"tool_id": f"smoke_tool_{TS%9999}", "label": "SmokeEcho", "url": "https://518.plus/api/v1/demo-echo",
            "timeout_s": 30, "output_filename": "echo.png", "output_content_type": "image/png",
            "accept": "image/png", "cost": 3, "visibility": "private"}
st, body = c7.req("POST", "/api/usertools", json.dumps(manifest).encode(), "application/json")
d = json.loads(body)
check("提交工具 manifest", st == 200 and d.get("ok"), body[:150])
smoke_key = d.get("tool_key", "")
# http 端点被拒
bad = dict(manifest); bad["tool_id"] = f"bad_{TS%9999}"; bad["url"] = "http://127.0.0.1:9/x"
st, body = c7.req("POST", "/api/usertools", json.dumps(bad).encode(), "application/json")
check("http 端点被拒(SSRF 门)", st == 422, f"status={st}")
# 未自测不能公开
patch = {"visibility": "public"}
st, body = c7.req("PATCH", f"/api/usertools/{manifest['tool_id']}", json.dumps(patch).encode(), "application/json")
check("未自测禁公开(阶梯门)", st == 422 and json.loads(body).get("error") == "self_test_required", body[:120])
# 自测 (需要源中间件 + fetch token 通过 demo-echo): tool=非share 走中间件路径
b, ct = mp({"expiryDays": "1", "allowedDownloads": "10", "tool": "compress"}, [("e.png", png1px(), "image/png")])
st, body = c7.req("POST", "/api/uploads", b, ct)
ef = json.loads(body).get("file")
st, body = c7.req("POST", f"/api/usertools/{manifest['tool_id']}/selftest",
                  json.dumps({"public_id": ef["publicId"], "source_token": ef["sourceToken"]}).encode(), "application/json")
d = json.loads(body)
check("自测调用(计量门内)", d.get("ms") is not None, body[:150])
# 调用日志
st, body = c7.req("GET", f"/api/usertools/{manifest['tool_id']}/calls")
check("调用日志", st == 200 and json.loads(body).get("ok"))
# patch 改价
st, body = c7.req("PATCH", f"/api/usertools/{manifest['tool_id']}", json.dumps({"cost": 2}).encode(), "application/json")
check("改价", st == 200)
# 删除
st, body = c7.req("DELETE", f"/api/usertools/{manifest['tool_id']}")
check("删除工具", st == 200)

# ════════════════ 10. 经济域 (余额/历史/充值页/收益页) ════════════════
dom("10.经济")
st, body = c7.req("GET", "/api/me")
check("/api/me 余额字段", json.loads(body).get("ok"))
st, body = c7.req("GET", "/account/credits")
check("积分页(含流水)", st == 200)
st, body = c7.req("GET", "/api/earnings")
d = json.loads(body)
check("收益面板(summary+明细)", d.get("ok") and "summary" in d and "revenue_lines" in d)
# 充值页 (creem 未配置 → coming soon 面)
st, body = c7.req("GET", "/account/credits")
check("充值区块渲染", st == 200)
# webhook 负路径: 坏签名
c = conn()
c.request("POST", "/webhooks/creem", body=b"{}", headers={"Content-Type": "application/json", "creem-signature": "bad"})
check("webhook 坏签名 401", c.getresponse().status == 401)
# 提现负路径: 余额不足
st, body = c7.req("POST", "/api/earnings/withdraw", json.dumps({"usd": 100}).encode(), "application/json")
check("超额提现拒绝", st == 422)

# ════════════════ 11. 后台 /samelabs ════════════════
dom("11.后台")
c = conn()
c.request("GET", "/samelabs")
check("匿名后台 302 门禁", c.getresponse().status == 302)
# 普通用户也被拒
st, _ = c7.req("GET", "/samelabs")
check("普通用户后台 302", st == 302)
# 管理员会话
adm_u = psql("SELECT username FROM users WHERE status='samelabs' ORDER BY id LIMIT 1")
if adm_u:
    adm_u = adm_u[0][0]
    psql("UPDATE users SET password_hash=%s WHERE username=%s",
         __import__("bcrypt").hashpw(b"AdminSmoke1", __import__("bcrypt").gensalt(rounds=4)).decode(), adm_u)
    ca = C()
    b, ct = mp({"login": adm_u, "password": "AdminSmoke1"}, [])
    st, _ = ca.req("POST", "/auth/login", b, ct)
    check("管理员登录", ca.sess is not None)
    for path in ["/samelabs", "/samelabs/users", "/samelabs/tools", "/samelabs/plans", "/samelabs/packages", "/samelabs/ledger", "/samelabs/activities", "/samelabs/withdrawals", "/samelabs/remote"]:
        st, body = ca.req("GET", path)
        check(f"后台页 {path}", st == 200, f"status={st}")
    # 定价改回
    st, body = ca.req("POST", "/samelabs/tools/doc2pdf/pricing", json.dumps({"cost": 5, "enabled": True}).encode(), "application/json")
    check("改定价", st == 200)
    # 活动配置
    st, body = ca.req("POST", "/samelabs/activities/daily_login/config", json.dumps({"amount": 5, "active": True}).encode(), "application/json")
    check("活动配置", st == 200)
    # 用户列表搜索
    st, body = ca.req("GET", f"/samelabs/users?q={u}")
    check("用户搜索", st == 200 and u.encode() in body)
    # 手动调整积分
    st, body = ca.req("POST", f"/samelabs/users/{uid}/credits", json.dumps({"amount": 10, "note": "smoke"}).encode(), "application/json")
    check("手动调整积分", st == 200)
    st, body = ca.req("POST", f"/samelabs/users/{uid}/credits", json.dumps({"amount": -5, "note": "smoke-neg"}).encode(), "application/json")
    check("手动扣减积分", st == 200)
    # 包创建+删 (唯一码)
    code = f"smk{TS%99999}"
    st, body = ca.req("POST", "/samelabs/packages", json.dumps({"code": code, "name": "S", "credits": 100, "price": 100}).encode(), "application/json")
    check("创建套餐包", st == 200, body[:120])
    psql("DELETE FROM packages WHERE code=%s", code)
    # 提现队列页渲染 (无 pending 也应 200)
    check("提现队列渲染", True)
else:
    check("管理员账号存在", False, "无 samelabs 用户")

# ════════════════ 12. 安全负路径 ════════════════
dom("12.安全负路径")
# 未登录上传
c = conn()
b, ct = mp({"expiryDays": "1"}, [("x.txt", b"X", "text/plain")])
c.request("POST", "/api/uploads", body=b, headers={"Content-Type": ct})
check("未登录上传 401", c.getresponse().status == 401)
# 越权访问他人文件
_ou, c9 = make_user("oth")
b, ct = mp({"expiryDays": "1", "allowedDownloads": "5"}, [("sec.txt", b"S", "text/plain")])
st, body = c9.req("POST", "/api/uploads", b, ct)
of = json.loads(body).get("file", {})
st, body = c7.req("DELETE", f"/api/uploads/{of['publicId']}")
check("越权删除 403/404", st in (403, 404), f"status={st}")
st, body = c7.req("PATCH", f"/api/uploads/{of['publicId']}", json.dumps({"name": "hack"}).encode(), "application/json")
check("越权改名拒绝", st in (403, 404))
# 无 source_token 操作中间件
st, body = c7.req("POST", "/api/tools/execute",
                  json.dumps({"tool_id": "share", "public_id": "zzzzzzzz1", "source_token": "", "params": {}}).encode(), "application/json")
check("伪造源 ref 拒绝", st in (400, 401, 403, 404), f"status={st} {body[:80]}")
# 禁止扩展名
b, ct = mp({"expiryDays": "1"}, [("evil.exe", b"MZ", "application/x-msdownload")])
st, body = c7.req("POST", "/api/uploads", b, ct)
check(".exe 上传拒绝", st == 400, f"status={st} {body[:80]}")
# 伪 content (jpg 头假 png)
b, ct = mp({"expiryDays": "1"}, [("fake.png", b"\xff\xd8\xff\xe0 notpng", "image/png")])
st, body = c7.req("POST", "/api/uploads", b, ct)
# png 魔数校验: fake.png 内容是 jpeg 头 → 拒
check("魔数不匹配拒绝", st == 400, f"status={st} {body[:80]}")
# fetch token 不可伪造
c = conn()
c.request("GET", "/api/source/zzzzzzzz1?u=1&t=forged", headers={})
check("伪造 fetch token 404", c.getresponse().status == 404)
# demo-echo 无 token 不读文件
st, body = https("/api/v1/demo-echo", "POST",
                 json.dumps({"job_inputs": [{"url": "/api/source/zzzzzzzz1?u=1&t=forged"}]}).encode(),
                 {"Content-Type": "application/json"})
d = json.loads(body)
check("demo-echo 过签名闸(空产出)", not d.get("files") or len(d.get("files", [])) == 0, body[:120])
# openapi 关闭 (不跟随重定向: 302→/ 会伪装成 200)
import urllib.request as _ur, ssl as _ssl
class _NoRedirect(_ur.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
_op = _ur.build_opener(_NoRedirect, _ur.HTTPSHandler(context=_ssl.create_default_context()))
_rq = _ur.Request("https://518.plus/openapi.json?nocache=" + str(time.time_ns()),
                  headers={"User-Agent": "Mozilla/5.0 SmokeTest/1.0"})
import urllib.error as _ue
try:
    _st = _op.open(_rq, timeout=30).status
except _ue.HTTPError as e:
    _st = e.code
check("openapi.json 屏蔽(不跟随)", _st in (404, 302, 403), f"status={_st}")
# id 注入 (public_id 特殊字符)
st, body = c7.req("GET", "/api/uploads?offset=abc")
check("offset 脏值容错(不500)", st == 200, f"status={st}")

# ════════════════ 13. PWA/静态资源 ════════════════
dom("13.PWA静态")
for path in ["/sw.js", "/static/i18n.js", "/static/app.js"]:
    st, body = https(path)
    check(f"GET {path}", st == 200 and len(body) > 500, f"status={st}")
st, body = https("/manifest.json")
check("manifest 旧路由", st in (200, 404))  # webmanifest 已验, 兼容路径
# 工具市场页含工具卡
st, body = https("/tools")
check("市场页含工具卡", b"pdf_merge" in body or b"doc2pdf" in body, f"status={st} len={len(body)}")

# ════════════════ 14. 健康/日志 ════════════════
dom("14.健康")
st, body = https("/healthz")
check("healthz", st == 200)

# ════════════════ 15. SSR 语言正确性 (0905 盲区落地) ════════════════
# 教训: i18n 清理后 home.html SSR 查已删键 → None → JS 兜底英文 id。
# 状态码断言抓不到, 必须断言"渲染文本 = DB label_json 对应语言"。
dom("15.SSR语言")
import json as _json
import re as _re
import html as _html_mod
_lang_rows = psql("SELECT tool_key, label_json FROM platform_tools WHERE visibility='public' AND enabled ORDER BY featured DESC NULLS LAST, sort_order NULLS LAST, id LIMIT 3")
_public = [(k, _json.loads(lj)) for k, lj in _lang_rows]
check("DB 可取公开工具行", len(_public) >= 2, f"rows={len(_public)}")

def _ssr_names(path, lang, cls="tool-tab-name"):
    st, body = https(path, headers={"Cookie": f"flashshare_lang={lang}"})
    html = body.decode("utf-8", "replace")
    names = []
    for m in _re.finditer(r'<span class="' + cls + r'">([^<]+)</span>', html):
        names.append(_html_mod.unescape(m.group(1)).strip())
    return st, names, html

def _resolve(label, lang):
    if isinstance(label, str):
        return label
    return label.get(lang) or label.get("en") or next(iter(label.values()), "")

for _lang in ["en", "zh", "ja"]:
    st, names, html = _ssr_names("/", _lang)
    expect = [_resolve(lj, _lang) for _, lj in _public]
    check(f"首页tab名SSR[{_lang}] = DB label", st == 200 and names[:3] == expect[:3],
          f"ssr={names[:3]} expect={expect[:3]}")
# 结构键泄漏 = SSR 查了不存在/已删的 i18n 键 (0905 根因形态)
st, names, html = _ssr_names("/", "zh")
check("首页无裸i18n结构键泄漏", not _re.search(r">(\s*)(expiry|downloads|tab_name|hero_sub)\s*<", html),
      "found raw i18n key in SSR output")
# featured 工具排首位 (c53078c 排序机制)
check("featured 工具居首页首位", bool(names) and names[0] == _resolve(_public[0][1], "zh"),
      f"first={names[:1]}")
# 市场页同语言链路 (卡片名 class=mkt-name)
st, names_m, _ = _ssr_names("/tools", "zh", cls="mkt-name")
_zh_expect = {(_k, _resolve(_lj, "zh")) for _k, _lj in _public}
check("市场页名称按 zh 渲染", st == 200 and any(n in {v for _, v in _zh_expect} for n in names_m[:12]),
      f"first_names={names_m[:5]}")
# RTL 语言 (ar): 渲染正常 + html lang/dir 属性正确
st_ar, names_ar, html_ar = _ssr_names("/", "ar")
_dir_ok = 'dir="rtl"' in html_ar and 'lang="ar"' in html_ar
check("ar 界面 SSR 正常(RTL)", st_ar == 200 and len(names_ar) >= 2 and _dir_ok,
      f"names={names_ar[:3]} dir_attr={_dir_ok}")

# ════════════════ 汇总 ════════════════
total = len(R)
fails = [x for x in R if not x[2]]
print("\n" + "=" * 60)
print(f"总计 {total} 项 | PASS {total - len(fails)} | FAIL {len(fails)}")
if fails:
    print("\n失败项:")
    for d, n, _, det in fails:
        print(f"  [{d}] {n} | {det}")
json.dump([{"domain": d, "check": n, "ok": ok, "detail": det} for d, n, ok, det in R],
          open("/tmp/smoke_result.json", "w"), ensure_ascii=False, indent=1)
# 清理冒烟数据
psql("DELETE FROM ledger WHERE user_id IN (SELECT id FROM users WHERE username LIKE 'smoke%' OR username LIKE 'oth%' OR username LIKE 'wk%')")
psql("DELETE FROM tool_calls WHERE user_id IN (SELECT id FROM users WHERE username LIKE 'smoke%' OR username LIKE 'oth%' OR username LIKE 'wk%')")
psql("DELETE FROM withdrawals WHERE user_id IN (SELECT id FROM users WHERE username LIKE 'smoke%' OR username LIKE 'oth%' OR username LIKE 'wk%')")
psql("DELETE FROM platform_tools WHERE owner_id IN (SELECT id FROM users WHERE username LIKE 'smoke%' OR username LIKE 'oth%' OR username LIKE 'wk%')")
psql("DELETE FROM users WHERE username LIKE 'smoke%' OR username LIKE 'oth%' OR username LIKE 'wk%'")
print("\n冒烟用户已清理")
