#!/usr/bin/env sh
# D1 E2E: MCP /mcp — initialize/list/call(带b64文件)/replay/ping/notification
set -e
cd /opt/flashshare
sudo /usr/bin/python3 - <<'PYEOF'
import http.client, io, json, time, zipfile, subprocess, base64
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

class C:
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

class M:  # MCP client
    def __init__(self, token): self.token=token; self.i=0
    def call(self, method, params=None, notify=False):
        self.conn=http.client.HTTPConnection(*HOST,timeout=180)
        msg={"jsonrpc":"2.0","method":method}
        if params is not None: msg["params"]=params
        if not notify:
            self.i+=1; msg["id"]=self.i
        h={'Content-Type':'application/json','Accept':'application/json, text/event-stream','Authorization':f'Bearer {self.token}'}
        self.conn.request('POST','/mcp',body=json.dumps(msg).encode(),headers=h)
        r=self.conn.getresponse(); d=r.read(); self.conn.close()
        return r.status, (json.loads(d) if d else None)

c=C(); u=f"mc{int(time.time())%100000}"
b,ct=mp({'username':u,'password':'Test12345','password_confirm':'Test12345','email':f'{u}@t.co'},[]); c.post('/auth/register',b,ct)
b,ct=mp({'login':u,'password':'Test12345'},[]); c.post('/auth/login',b,ct)
st,d=c.post('/api/tokens',json.dumps({'name':'mcp-e2e'}).encode(),'application/json')
tok=json.loads(d)['token']['token']

m=M(tok)

# 1. initialize 握手
st,r=m.call('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'e2e','version':'0'}})
assert st==200 and r['result']['protocolVersion']=='2025-06-18' and r['result']['serverInfo']['name']=='518plus',(st,r)
print("1. initialize OK (2025-06-18)")

# 2. initialized notification -> 202
st,r=m.call('notifications/initialized',notify=True)
assert st==202 and r is None,(st,r)
print("2. notification -> 202 OK")

# 3. ping
st,r=m.call('ping')
assert st==200 and r['result']=={}
print("3. ping OK")

# 4. tools/list: 10工具+schema
st,r=m.call('tools/list')
tools=r['result']['tools']
ids={t['name'] for t in tools}
assert 'doc2pdf' in ids and 'catalog_search' in ids,(len(tools),ids)
assert len(tools) >= 11, (len(tools), ids)  # 10 一方+meta, 数据态目录可含公开用户工具
d2=[t for t in tools if t['name']=='doc2pdf'][0]
assert d2['inputSchema']['required']==['file_b64','filename']
print(f"4. tools/list OK ({len(tools)} tools incl catalog_search meta, schema correct)")

# 4b. initialize declares listChanged; instructions carry catalog version
st,r=m.call('initialize',{'protocolVersion':'2025-03-26'})
assert r['result']['capabilities']['tools']['listChanged'] is True
assert 'Catalog version' in r['result']['instructions']
st,r=m.call('tools/call',{'name':'catalog_search','arguments':{'q':'pdf'}})
import json as _j
hits=_j.loads(r['result']['content'][0]['text'])['tools']
assert 'doc2pdf' in hits and 'img2pdf' in hits, hits
print("4b. listChanged + catalog_version + catalog_search meta OK")

# 5. tools/call doc2pdf: b64真转换
def minimal_docx(text):
    ct='<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    rels='<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    doc=f'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr('[Content_Types].xml',ct); z.writestr('_rels/.rels',rels); z.writestr('word/document.xml',doc)
    return buf.getvalue()

src=base64.b64encode(minimal_docx('mcp tool call test')).decode()
st,r=m.call('tools/call',{'name':'doc2pdf','arguments':{'file_b64':src,'filename':'mcp.docx','client_ref':'mcp-e2e-1','params':{}}})
assert st==200 and not r['result']['isError'],json.dumps(r)[:300]
out=json.loads(r['result']['content'][0]['text'])
assert out['result']['filename'].endswith('.pdf') and out['call_id']>0
print(f"5. tools/call OK: {out['result']['filename']} call_id={out['call_id']}")

# 6. 同client_ref重放: replay=true 同结果
st,r=m.call('tools/call',{'name':'doc2pdf','arguments':{'file_b64':src,'filename':'mcp.docx','client_ref':'mcp-e2e-1','params':{}}})
out2=json.loads(r['result']['content'][0]['text'])
assert out2['replay'] and out2['result']['publicId']==out['result']['publicId']
print("6. replay idempotent OK")

# 7. 失败工具调用: isError result(非协议错)
bad=base64.b64encode(b'PK\x03\x04 not a real zip').decode()
st,r=m.call('tools/call',{'name':'doc2pdf','arguments':{'file_b64':bad,'filename':'bad.docx','client_ref':'mcp-e2e-2','params':{}}})
assert st==200 and r['result']['isError'], json.dumps(r)[:200]
print("7. tool failure -> isError result OK")

# 8. 无效方法 -> -32601
st,r=m.call('resources/list')
assert r['error']['code']==-32601
print("8. unknown method -32601 OK")

# 9. GET/DELETE -> 405 (stateless)
conn=http.client.HTTPConnection(*HOST); conn.request('GET','/mcp'); rr=conn.getresponse()
assert rr.status==405
conn=http.client.HTTPConnection(*HOST); conn.request('DELETE','/mcp'); rr=conn.getresponse()
assert rr.status==405
print("9. GET/DELETE 405 (stateless) OK")

cleanup_user(u)
print("cleaned")
print("D1 ALL MCP E2E PASSED")
PYEOF