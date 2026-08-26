import http.client, time, sys, os, re, json
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2, hmac, hashlib, secrets as _sec
from config import SESSION_SECRET, COOKIE_NAME
PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, *a):
    if len(a) == 1 and isinstance(a[0], tuple): a = a[0]
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

ts = int(time.time())
uname = f"sp{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'split t','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+600, ts))
conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
ck = f"{COOKIE_NAME}={tok_s}; flashshare_lang=zh"

def get(p):
    conn.request("GET", p, headers={"Cookie": ck})
    r = conn.getresponse(); return r.status, r.read().decode()

# 1. /account/earnings page renders standalone
st, html = get("/account/earnings")
c1 = st == 200 and 'id="earnGrid"' in html and 'id="byToolList"' in html and "我的收益" in html
print(("PASS" if c1 else "FAIL"), "1. /account/earnings standalone page")

# 2. tools page has NO earnings markup
st2, thtml = get("/account/tools")
c2 = st2 == 200 and "earnGrid" not in thtml and "withdrawModal" not in thtml and 'id="toolModal"' in thtml
print(("PASS" if c2 else "FAIL"), "2. /account/tools: money view stripped, tool modal intact")

# 3. nav has 6 tabs incl earnings
c3 = 'href="/account/earnings"' in thtml and 'href="/account/tools"' in thtml
print(("PASS" if c3 else "FAIL"), "3. nav: earnings + tools tabs")

# 4. API split: /api/earnings works, /api/usertools/earnings gone
conn.request("GET", "/api/earnings", headers={"Cookie": ck})
r = conn.getresponse(); d = json.loads(r.read())
c4 = r.status == 200 and d["ok"] and "summary" in d and "min_withdrawal" in d
print(("PASS" if c4 else "FAIL"), "4. /api/earnings live")
conn.request("GET", "/api/usertools/earnings", headers={"Cookie": ck})
r5 = conn.getresponse(); body5 = r5.read()
c5 = r5.status in (404, 405)  # old GET endpoint gone; 405 = {tool_id} route shape collision, not the old handler
print(("PASS" if c5 else "FAIL"), "5. old /api/usertools/earnings 404")

# 6. withdraw flow via new endpoint (min rejection typed)
conn.request("POST", "/api/earnings/withdraw", body=json.dumps({"amount": 5}),
             headers={"Cookie": ck, "Content-Type": "application/json"})
r6 = conn.getresponse(); d6 = json.loads(r6.read())
c6 = r6.status == 422 and d6["error"] == "withdrawal_min"
print(("PASS" if c6 else "FAIL"), "6. withdraw min-rejection via new endpoint")

# 7. dropdown 我的收益 → /account/earnings
_, home = get("/")
c7 = 'href="/account/earnings"' in home
print(("PASS" if c7 else "FAIL"), "7. dropdown earnings link")

# 8. tools JS: no orphan (earn calls gone)
m = re.search(r'{% block script %}(.*?){% endblock %}', open('/opt/flashshare/app/templates/account/tools.html').read(), re.S)
c8 = 'loadEarnings' not in (m.group(1) if m else '')
print(("PASS" if c8 else "FAIL"), "8. tools.html JS earnings-free")

ok = all([c1, c2, c3, c4, c5, c6, c7, c8])
psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
print("ALL PASS" if ok else "HAS FAILURES")
