#!/usr/bin/env sh
# C1+C2 计量E2E: 定价开关/扣费/幂等重放/失败退款/免费工具不受影响
set -e
cd /opt/flashshare
sudo /usr/bin/python3 - <<'PYEOF'
import http.client, io, json, time, zipfile

HOST=("127.0.0.1",8000)
def mp(fields, files, boundary="----bnd789"):
    body=io.BytesIO()
    for k,v in fields.items(): body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for (fn,data,ct) in files:
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fn}"\r\nContent-Type: {ct}\r\n\r\n'.encode()+data+b'\r\n')
    body.write(f'--{boundary}--\r\n'.encode()); return body.getvalue(), f'multipart/form-data; boundary={boundary}'
class C:
    def __init__(self): self.conn=http.client.HTTPConnection(*HOST,timeout=180); self.sess=''
    def post(self,path,body,ct):
        h={'Content-Type':ct,'Origin':'http://127.0.0.1:8000'}
        if self.sess: h['Cookie']=self.sess
        self.conn.request('POST',path,body=body,headers=h); r=self.conn.getresponse(); d=r.read()
        sc=r.getheader('Set-Cookie')
        if sc: self.sess=sc.split(';')[0]
        return r.status,d
    def get(self,path): 
        self.conn.request('GET',path,headers={'Cookie':self.sess} if self.sess else {}); r=self.conn.getresponse(); return r.status,r.read()

import sys
import subprocess
# PG URL 从生产env读取(清理段用)
PG_URL = subprocess.run(['sh','-c','. /etc/flashshare.env && echo $DATABASE_URL'],capture_output=True,text=True).stdout.strip().replace('postgresql+psycopg://','postgresql://')
import os
os.environ['DATABASE_URL']=PG_URL
from tests.e2e_cleanup import cleanup_user

c=C(); u=f"mc{int(time.time())%100000}"
b,ct=mp({'username':u,'password':'Test12345','password_confirm':'Test12345','email':f'{u}@t.co'},[]); c.post('/auth/register',b,ct)
b,ct=mp({'login':u,'password':'Test12345'},[]); c.post('/auth/login',b,ct)

# admin helper: set pricing via DB (samelabs UI is C4; DB is the same source)
import psycopg2
def set_price(tool,cost,enabled=True):
    con=psycopg2.connect(os.environ.get('PG_URL'))
    cur=con.cursor()
    cur.execute("INSERT INTO pricing(tool_id,cost,enabled,updated_at) VALUES(%s,%s,%s,extract(epoch from now())::bigint) ON CONFLICT(tool_id) DO UPDATE SET cost=EXCLUDED.cost,enabled=EXCLUDED.enabled",(tool,cost,enabled))
    con.commit(); con.close()

import subprocess
PG_URL = subprocess.run(['sh','-c','. /etc/flashshare.env && echo $DATABASE_URL'],capture_output=True,text=True).stdout.strip().replace('postgresql+psycopg://','postgresql://')
os.environ['PG_URL']=PG_URL

# 1. doc2pdf 定价5积分
set_price('doc2pdf', 5)
def minimal_docx(text):
    ct='<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    rels='<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    doc=f'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr('[Content_Types].xml',ct); z.writestr('_rels/.rels',rels); z.writestr('word/document.xml',doc)
    return buf.getvalue()

b,ct=mp({'expiryDays':'1','allowedDownloads':'10','tool':'doc2pdf'},[('r.docx',minimal_docx('metered test'),'application/vnd.openxmlformats-officedocument.wordprocessingml.document')])
st,d=c.post('/api/uploads',b,ct); f=json.loads(d)['file']

def balance():
    con=psycopg2.connect(PG_URL); cur=con.cursor()
    cur.execute("SELECT COALESCE(SUM(delta),0) FROM ledger WHERE user_id=(SELECT id FROM users WHERE username=%s)",(u,))
    v=cur.fetchone()[0]; con.close(); return v

# 2. 首次带client_ref执行: daily5 - call5 = 0 (signup bonus decoupled 2026-08-27)
# seed spendable credits for the billed call (dual-pool: grants only)
psql_seed = ("INSERT INTO ledger (user_id, delta, balance_after, kind, ref_type, ref_id, idem_key, note, created_at) "
             "SELECT id, 100, 100, 'grant', 'test', 'seed', 'meter-seed:'||id, '', extract(epoch from now())::bigint "
             "FROM users WHERE username=%s")
con=psycopg2.connect(PG_URL); cur=con.cursor(); cur.execute(psql_seed,(u,)); con.commit(); con.close()
req=json.dumps({'tool_id':'doc2pdf','public_id':f['publicId'],'source_token':f['sourceToken'],'client_ref':'ref-e2e-001','params':{'preset':'auto'}}).encode()
st,d=c.post('/api/tools/execute',req,'application/json')
res=json.loads(d)
assert res['ok'], d[:200]
assert balance()==100, f"expected 100 (grant100+daily5-call5), got {balance()}"
print(f"2. grant100 + charge 5 -> balance {balance()} OK (call_id={res.get('call_id')})")

# 3. 同 client_ref 重放: 返回原结果不重扣
st,d=c.post('/api/tools/execute',req,'application/json')
res2=json.loads(d)
assert res2['ok'] and res2.get('replay') and res2['result']['publicId']==res['result']['publicId']
assert balance()==100
print("3. replay: same result, no re-charge OK")

# 4. 失败自动退款: 损坏的docx(伪zip头) -> err + 退款
b,ct=mp({'expiryDays':'1','allowedDownloads':'10','tool':'doc2pdf'},[('bad.docx',b'PK\x03\x04 this is not a real zip archive content at all','application/vnd.openxmlformats-officedocument.wordprocessingml.document')])
st,d=c.post('/api/uploads',b,ct)
up=json.loads(d)
if not up.get('ok'):
    # sniff 拦截在upload层 = 计量前, 换一个能过upload但转换必败的: 合法zip但非docx结构
    import zipfile as zf2
    buf=io.BytesIO()
    with zf2.ZipFile(buf,'w') as z: z.writestr('notword.txt','hello')
    b,ct=mp({'expiryDays':'1','allowedDownloads':'10','tool':'doc2pdf'},[('bad.docx',buf.getvalue(),'application/vnd.openxmlformats-officedocument.wordprocessingml.document')])
    st,d=c.post('/api/uploads',b,ct); up=json.loads(d)
fb=up['file']
assert fb, up
req_bad=json.dumps({'tool_id':'doc2pdf','public_id':fb['publicId'],'source_token':fb['sourceToken'],'client_ref':'ref-e2e-002','params':{}}).encode()
st,d=c.post('/api/tools/execute',req_bad,'application/json')
resb=json.loads(d)
assert not resb['ok'], d[:200]
assert balance()==100, f"expected 100 (fail auto-refunded), got {balance()}"
print(f"4. failed call auto-refunded -> balance {balance()} OK")

# 5. 复用失败过的 client_ref 重试成功(新源文件, 旧源已耗尽)
b,ct=mp({'expiryDays':'1','allowedDownloads':'10','tool':'doc2pdf'},[('r2.docx',minimal_docx('retry body'),'application/vnd.openxmlformats-officedocument.wordprocessingml.document')])
st,d=c.post('/api/uploads',b,ct); f2=json.loads(d)['file']
req_ok2=json.dumps({'tool_id':'doc2pdf','public_id':f2['publicId'],'source_token':f2['sourceToken'],'client_ref':'ref-e2e-002','params':{'preset':'auto'}}).encode()
st,d=c.post('/api/tools/execute',req_ok2,'application/json')
resr=json.loads(d)
assert resr['ok'] and not resr.get('replay'), d[:200]
assert balance()==95, f"retry charged 5 -> 95, got {balance()}"
print(f"5. retry-after-fail with same ref works, balance {balance()}")

# 6. 定价归零(免费)后执行不扣费
set_price('doc2pdf', 0)
b,ct=mp({'expiryDays':'1','allowedDownloads':'10','tool':'doc2pdf'},[('r3.docx',minimal_docx('free body'),'application/vnd.openxmlformats-officedocument.wordprocessingml.document')])
st,d=c.post('/api/uploads',b,ct); f3=json.loads(d)['file']
req_f=json.dumps({'tool_id':'doc2pdf','public_id':f3['publicId'],'source_token':f3['sourceToken'],'client_ref':'ref-e2e-003','params':{'preset':'auto'}}).encode()
st,d=c.post('/api/tools/execute',req_f,'application/json')
resf=json.loads(d)

assert resf['ok']
assert balance()==95, f"free call should not charge, got {balance()}"
print("6. cost=0 free call OK")

# 7. 工具禁用 -> 403
set_price('doc2pdf', 5, enabled=False)
st,d=c.post('/api/tools/execute',req_f,'application/json')
resd=json.loads(d)
assert st==403 and resd['error']=='tool_disabled'
set_price('doc2pdf', 5, enabled=True)
print("7. disabled tool rejected 403, re-enabled OK")

cleanup_user(u)
print("cleaned")
print("C1+C2 ALL METERING E2E PASSED")
PYEOF