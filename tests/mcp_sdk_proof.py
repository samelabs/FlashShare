#!/usr/bin/env python3
"""D1 cross-harness proof: official MCP SDK client (streamable-http) -> live /mcp."""
import asyncio, io, json, time, zipfile, base64

async def main():
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.client.session import ClientSession

    # bootstrap: fresh user + token via plain HTTP (session bootstrap only)
    import http.client
    def mp(fields, boundary="----bnd789"):
        body=io.BytesIO()
        for k,v in fields.items(): body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        body.write(f'--{boundary}--\r\n'.encode()); return body.getvalue(), f'multipart/form-data; boundary={boundary}'
    class C:
        def __init__(self): self.conn=http.client.HTTPConnection("127.0.0.1",8000,timeout=60); self.sess=''
        def post(self,path,body,ct):
            h={'Content-Type':ct,'Origin':'http://127.0.0.1:8000'}
            if self.sess: h['Cookie']=self.sess
            self.conn.request('POST',path,body=body,headers=h); r=self.conn.getresponse(); d=r.read()
            sc=r.getheader('Set-Cookie')
            if sc: self.sess=sc.split(';')[0]
            return r.status,d
    c=C(); u=f"sdk{int(time.time())%100000}"
    b,ct=mp({'username':u,'password':'Test12345','password_confirm':'Test12345','email':f'{u}@t.co'}); c.post('/auth/register',b,ct)
    b,ct=mp({'login':u,'password':'Test12345'}); c.post('/auth/login',b,ct)
    st,d=c.post('/api/tokens',json.dumps({'name':'sdk-proof'}).encode(),'application/json')
    tok=json.loads(d)['token']['token']

    url="http://127.0.0.1:8000/mcp"
    headers={"Authorization": f"Bearer {tok}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as s:
            init=await s.initialize()
            print(f"1. SDK initialize OK: server={init.serverInfo.name} proto={init.protocolVersion}")
            tools=await s.list_tools()
            print(f"2. SDK list_tools OK: {len(tools.tools)} tools")
            def minimal_docx(text):
                ct2='<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
                rels='<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
                doc=f'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
                buf=io.BytesIO()
                with zipfile.ZipFile(buf,'w') as z:
                    z.writestr('[Content_Types].xml',ct2); z.writestr('_rels/.rels',rels); z.writestr('word/document.xml',doc)
                return buf.getvalue()
            src=base64.b64encode(minimal_docx('sdk cross harness')).decode()
            r=await s.call_tool('doc2pdf',{'file_b64':src,'filename':'sdk.docx','client_ref':f'sdk-{time.time()}'})
            assert not r.isError
            out=json.loads(r.content[0].text)
            print(f"3. SDK call_tool OK: {out['result']['filename']} call_id={out['call_id']}")
    print("D1 SDK CROSS-HARNESS PROOF PASSED")

asyncio.run(main())
