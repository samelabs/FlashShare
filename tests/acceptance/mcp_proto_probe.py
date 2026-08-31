import http.client, time, sys, os, json
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2, secrets as _sec
from identity.apitokens import create_token

PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
con = psycopg2.connect(PG); cur = con.cursor()
cur.execute("SELECT id FROM users ORDER BY id LIMIT 1")
uid = cur.fetchone()[0]
con.close()

t = create_token(uid, "mcp-proto-probe")
tok = t["token"]
conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)

def rpc(method, params=None, _id=1):
    body = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        body["params"] = params
    conn.request("POST", "/mcp", body=json.dumps(body),
                 headers={"Content-Type": "application/json",
                          "Authorization": f"Bearer {tok}"})
    r = conn.getresponse()
    return r.status, json.loads(r.read())

# 1. initialize: listChanged declared + version in instructions
st, d = rpc("initialize", {"protocolVersion": "2025-03-26"})
caps = d["result"]["capabilities"]["tools"]
c1 = st == 200 and caps.get("listChanged") is True
print(("PASS" if c1 else "FAIL"), "1. initialize: tools.listChanged=true")
c1b = "Catalog version" in d["result"]["instructions"]
print(("PASS" if c1b else "FAIL"), "1b. instructions carry catalog version")

# 2. tools/list page 1 (page_size 50; catalog is 10 → one page, no cursor)
st, d = rpc("tools/list", {})
tools = d["result"]["tools"]
names = [t["name"] for t in tools]
c2 = st == 200 and len(tools) == 11  # 10 + catalog_search meta
print(("PASS" if c2 else "FAIL"), f"2. tools/list returns {len(tools)} (10 + meta)")

# 3. catalog_search meta tool callable
st, d = rpc("tools/call", {"name": "catalog_search", "arguments": {"q": "pdf"}})
out = json.loads(d["result"]["content"][0]["text"])
c3 = st == 200 and any("pdf" in n for n in out["tools"])
print(("PASS" if c3 else "FAIL"), f"3. catalog_search('pdf') → {out['tools'][:4]}")

# 3b. zh label search
st, d = rpc("tools/call", {"name": "catalog_search", "arguments": {"q": "压缩"}})
out = json.loads(d["result"]["content"][0]["text"])
c3b = st == 200 and len(out["tools"]) >= 1
print(("PASS" if c3b else "FAIL"), f"3b. zh label search 压缩 → {out['tools'][:4]}")

# 4. pagination: force cursor by invalid cursor rejection
st, d = rpc("tools/list", {"cursor": "abc"})
c4 = "error" in d and d["error"]["code"] == -32602
print(("PASS" if c4 else "FAIL"), "4. invalid cursor → -32602")

# 5. catalog_version stability + sensitivity
from governance.catalog import catalog_version
v1 = catalog_version()
cur = psycopg2.connect(PG).cursor()
cur2 = psycopg2.connect(PG)
cu = cur2.cursor()
cu.execute("UPDATE platform_tools SET updated_at = updated_at + 1 WHERE tool_key = 'ico'")
cur2.commit()
v2 = catalog_version()
c5 = v1 != v2
print(("PASS" if c5 else "FAIL"), f"5. catalog_version bumps on change ({v1} → {v2})")
cu.execute("UPDATE platform_tools SET updated_at = updated_at - 1 WHERE tool_key = 'ico'")
cur2.commit()

ok = all([c1, c1b, c2, c3, c3b, c4, c5])
from identity.apitokens import revoke_token
revoke_token(uid, t["id"])
print("ALL PASS" if ok else "HAS FAILURES")
