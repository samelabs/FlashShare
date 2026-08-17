#!/usr/bin/env sh
# C3 E2E: /api/v1 五端点全链路 (Bearer-only) — me/tools/uploads/execute/fetch
set -e
cd /opt/flashshare
sudo /usr/bin/python3 - <<'PYEOF'
import http.client, io, json, time, zipfile, subprocess
import os

PG_URL = subprocess.run(['sh','-c','. /etc/flashshare.env && echo $DATABASE_URL'],capture_output=True,text=True).stdout.strip().replace('postgresql+psycopg://','postgresql://')
os.environ['DATABASE_URL']=PG_URL
from tests.e2e_cleanup import cleanup_user

HOST=("127.0.0.1",8000)
def mp(fields, files, boundary="----bnd789"):
    body=io.BytesIO()
    for k,v in fields.items(): body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for (fn,data,ct) in files:
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fn}"\r\nContent-Type: {ct}\r\n\r\n'.encode()+data+b'\r\n')
    body.write(f'--{boundary}--\r\n'.encode()); return body.getvalue(), f'multipart/form-data; boundary={boundary}'

class B:  # Bearer-only client (v1 must reject cookies)
    def __init__(self, token): self.token=token; self.conn=http.client.HTTPConnection(*HOST,timeout=180)
    def req(self,method,path,body=None,ct=None,extra=None):
        h={'Authorization':f'Bearer {self.token}'}
        if ct: h['Content-Type']=ct
        if extra: h.update(extra)
        self.conn.request(method,path,body=body,headers=h); r=self.conn.getresponse(); return r.status,r.read()
    def get(self,path): return self.req('GET',path)
    def post(self,path,body,ct='application/json',extra=None): return self.req('POST',path,body,ct,extra)

class C:  # cookie client (session bootstrap only)
    def __init__(self): self.conn=http.client.HTTPConnection(*HOST,timeout=60); self.sess=''
    def post(self,path,body,ct):
        h={'Content-Type':ct,'Origin':'http://127.0.0.1:8000'}
        if self.sess: h['Cookie']=self.sess
        self.conn.request('POST',path,body=body,headers=h); r=self.conn.getresponse(); d=r.read()
        sc=r.getheader('Set-Cookie')
        if sc: self.sess=sc.split(';')[0]
        return r.status,d
    def get(self,path):
        self.conn.request('GET',path,headers={'Cookie':self.sess} if self.sess else {}); r=self.conn.getresponse(); return r.status,r.read()

# bootstrap: session 用户 + 创建 token
c=C(); u=f"v1{int(time.time())%100000}"
b,ct=mp({'username':u,'password':'Test12345','password_confirm':'Test12345','email':f'{u}@t.co'},[]); c.post('/auth/register',b,ct)
b,ct=mp({'login':u,'password':'Test12345'},[]); c.post('/auth/login',b,ct)
st,d=c.post('/api/tokens',json.dumps({'name':'v1-e2e'}).encode(),'application/json')
tok=json.loads(d)['token']['token']

# 0. cookie(无Bearer)被 v1 拒绝 — token-only 纪律
st,d=c.get('/api/v1/me')
assert st==401, (st,d[:100])
print("0. session cookie rejected by /api/v1 (token-only) OK")

v=B(tok)

# 1. me: 身份+余额
st,d=v.get('/api/v1/me')
me=json.loads(d)
assert st==200 and me['ok'] and me['user']['username']==u and me['credits']==0,(st,d[:200])
assert 'email' not in me['user']
print(f"1. /me ok: {me['user']['username']} credits={me['credits']} (no email leak) OK")

# 2. tools: 目录+定价
st,d=v.get('/api/v1/tools')
tools=json.loads(d)['tools']
ids={t['id'] for t in tools}
assert 'doc2pdf' in ids and 'pdf_merge' in ids and len(tools)>=10
d2=[t for t in tools if t['id']=='doc2pdf'][0]
assert 'cost' in d2 and 'label' in d2
print(f"2. /tools ok: {len(tools)} tools, doc2pdf cost={d2['cost']} OK")

# 3. uploads: 直传源文件
def minimal_docx(text):
    ct='<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    rels='<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    doc=f'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr('[Content_Types].xml',ct); z.writestr('_rels/.rels',rels); z.writestr('word/document.xml',doc)
    return buf.getvalue()

src=minimal_docx('v1 api execute test')
st,d=v.req('POST','/api/v1/uploads',src,'application/vnd.openxmlformats-officedocument.wordprocessingml.document',{'x-filename':'v1test.docx'})
f=json.loads(d)['file']
assert st==200 and f['sourceToken'] and f['publicId'],(st,d[:200])
print(f"3. /uploads ok: {f['publicId']} ({f['size']}B) OK")

# 4. execute: client_ref 必填+幂等
body=json.dumps({'tool_id':'doc2pdf','public_id':f['publicId'],'source_token':f['sourceToken'],'client_ref':'v1-e2e-ref-1','params':{}}).encode()
st,d=v.post('/api/v1/execute',body)
res=json.loads(d)
assert res['ok'], (st,d[:300])
assert res['result']['filename'].endswith('.pdf') and not res.get('replay')
print(f"4a. /execute ok: {res['result']['filename']} call_id={res.get('call_id')} OK")
st,d=v.post('/api/v1/execute',body)
res2=json.loads(d)
assert res2['ok'] and res2.get('replay') and res2['result']['publicId']==res['result']['publicId']
print("4b. /execute replay idempotent OK")
# 无 client_ref 拒绝
st,d=v.post('/api/v1/execute',json.dumps({'tool_id':'doc2pdf','public_id':'x','source_token':'y','params':{}}).encode())
assert st==422
print("4c. missing client_ref rejected 422 OK")

# 5. fetch: 取回结果字节 + meta
pid=res['result']['publicId']
st,d=v.get(f'/api/v1/uploads/{pid}?meta=1')
meta=json.loads(d)
assert st==200 and meta['ok'] and meta['file']['filename'].endswith('.pdf')
st,d=v.get(f'/api/v1/uploads/{pid}')
assert st==200 and d[:5]==b'%PDF-'
print(f"5. /fetch ok: meta + {len(d)}B PDF bytes OK")

# 6. 他人资源不可读(真实第二用户+其token; 新连接避免stale keep-alive)
u2=f"v1b{int(time.time())%100000}"
c2=C()
b,ct=mp({'username':u2,'password':'Test12345','password_confirm':'Test12345','email':f'{u2}@t.co'},[]); c2.post('/auth/register',b,ct)
b,ct=mp({'login':u2,'password':'Test12345'},[]); c2.post('/auth/login',b,ct)
st,d=c2.post('/api/tokens',json.dumps({'name':'v1-e2e-2'}).encode(),'application/json')
tok2=json.loads(d)['token']['token']
v2=B(tok2)
st,d=v2.get(f'/api/v1/uploads/{pid}')
assert st==403,(st,d[:100])
print("6. cross-user fetch forbidden 403 OK")

cleanup_user(u)
cleanup_user(u2)
print("cleaned")
print("C3 ALL API-V1 E2E PASSED")
PYEOF