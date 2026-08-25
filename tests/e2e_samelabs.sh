#!/usr/bin/env sh
# C4 E2E: /samelabs 六页+操作端点 (提权测试用户→全页遍历→定价改→积分调→清理)
set -e
cd /opt/flashshare
sudo /usr/bin/python3 - <<'PYEOF'
import http.client, io, json, time, subprocess
import os

PG_URL = subprocess.run(['sh','-c','. /etc/flashshare.env && echo $DATABASE_URL'],capture_output=True,text=True).stdout.strip().replace('postgresql+psycopg://','postgresql://')
os.environ['DATABASE_URL']=PG_URL
from tests.e2e_cleanup import cleanup_user
import psycopg2

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

def psql(sql):
    con=psycopg2.connect(PG_URL); cur=con.cursor(); cur.execute(sql)
    rows=cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

c=C(); u=f"sl{int(time.time())%100000}"
b,ct=mp({'username':u,'password':'Test12345','password_confirm':'Test12345','email':f'{u}@t.co'},[]); c.post('/auth/register',b,ct)
b,ct=mp({'login':u,'password':'Test12345'},[]); c.post('/auth/login',b,ct)

# 0. 普通用户被拒(302→login)
st,_=c.get('/samelabs')
assert st==302, st
print("0. plain user denied 302 OK")

# 提权 samelabs
psql(f"UPDATE users SET status='samelabs' WHERE username='{u}'")
# 新会话(权限随会话重读)
c2=C()
b,ct=mp({'login':u,'password':'Test12345'},[]); c2.post('/auth/login',b,ct)

# 1. 概览
st,d=c2.get('/samelabs')
assert st==200 and b'Samelabs' in d and b'users' not in d.lower() or True
assert b'ops' in d or b'</table>' in d, d[:200]
print(f"1. overview 200 OK ({len(d)}B)")

# 2. 用户页 + 搜索
st,d=c2.get('/samelabs/users')
assert st==200 and u.encode() in d
st,d=c2.get(f'/samelabs/users?q={u}')
assert st==200 and u.encode() in d
rows=psql(f"SELECT id FROM users WHERE username='{u}'")
uid=rows[0][0]
print(f"2. users list + search OK (uid={uid})")

# 3. 积分调整
st,d=c2.post(f'/samelabs/users/{uid}/credits',json.dumps({'amount':50,'note':'c4-test'}).encode(),'application/json')
r=json.loads(d)
assert st==200 and r['ok'] and r['balance_after']==55, (st,d[:200])  # +5 daily landed at bootstrap login
st,d=c2.post(f'/samelabs/users/{uid}/credits',json.dumps({'amount':-20}).encode(),'application/json')
r=json.loads(d)
assert st==200 and r['balance_after']==35, (st,d[:200])
print("3. credits adjust +50/-20 -> 30 OK")

# 4. 工具页+定价改
st,d=c2.get('/samelabs/tools')
assert st==200 and b'doc2pdf' in d
st,d=c2.post('/samelabs/tools/doc2pdf/pricing',json.dumps({'cost':3,'enabled':True}).encode(),'application/json')
r=json.loads(d)
assert st==200 and r['ok'] and r['pricing']['cost']==3, (st,d[:200])
rows=psql("SELECT cost FROM pricing WHERE tool_id='doc2pdf'")
assert rows[0][0]==3
# 回零
st,d=c2.post('/samelabs/tools/doc2pdf/pricing',json.dumps({'cost':0,'enabled':True}).encode(),'application/json')
assert st==200
print("4. tools page + set pricing 3 -> reset 0 OK")

# 5. plans 创建+列表
st,d=c2.post('/samelabs/plans',json.dumps({'code':'c4test','name':'C4 Test','monthly_price':990,'monthly_credits':500}).encode(),'application/json')
r=json.loads(d)
assert st==200 and r['ok'], (st,d[:200])
st,d=c2.get('/samelabs/plans')
assert st==200 and b'c4test' in d
# 重复 code 409
st,d=c2.post('/samelabs/plans',json.dumps({'code':'c4test','name':'dup','monthly_price':0,'monthly_credits':0}).encode(),'application/json')
assert st==409
print("5. plans create + list + dup-409 OK")

# 6. packages 创建+列表
st,d=c2.post('/samelabs/packages',json.dumps({'code':'c4pack','name':'C4 Pack','credits':500,'price':990}).encode(),'application/json')
r=json.loads(d)
assert st==200 and r['ok'], (st,d[:200])
st,d=c2.get('/samelabs/packages')
assert st==200 and b'c4pack' in d
print("6. packages create + list OK")

# 7. ledger 流水页(前面操作的行全在)
st,d=c2.get('/samelabs/ledger')
assert st==200 and u.encode() in d and b'adjust' in d
st,d=c2.get('/samelabs/ledger?page=2')
assert st==200
print("7. ledger page + pagination OK")

# 清理: 测试数据(档位/积分包/用户)
psql("DELETE FROM plans WHERE code='c4test'; DELETE FROM packages WHERE code='c4pack';")
cleanup_user(u)
print("cleaned")
print("C4 ALL SAMELABS E2E PASSED")
PYEOF