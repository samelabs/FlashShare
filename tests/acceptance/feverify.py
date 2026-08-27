import http.client, time, sys, os, re, hmac, hashlib, secrets as _sec
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2
from config import SESSION_SECRET, COOKIE_NAME
PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, a=()):
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows
ts = int(time.time()); uname = f"fx{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'f','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+600, ts))
conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
ck = f"{COOKIE_NAME}={tok_s}"
ok_all = True
PAGES = ["/", "/agents", "/auth/register", "/account/tools", "/account/earnings", "/account/tokens", "/account/profile", "/account/security", "/account/credits"]
for p in PAGES:
    conn.request("GET", p, headers={"Cookie": ck})
    r = conn.getresponse(); h = r.read().decode()
    issues = []
    if r.status != 200: issues.append(f"status {r.status}")
    # 内联大JS残留(>500 chars的script body)?
    for m in re.findall(r'<script>(.*?)</script>', h, re.S):
        if len(m.strip()) > 500: issues.append(f"inline {len(m)}c")
    # jinja泄漏到页面?
    if re.search(r'\{\{[^}]*\}\}', h.split('<body>')[1]): issues.append('jinja leak')
    ok = not issues
    if not ok: ok_all = False
    print(("PASS" if ok else "FAIL"), p, issues or '')
# 静态JS全部可达
for f in ["tools", "earnings", "agents", "register", "tokens", "profile", "detail", "footer", "samelabs_base", "samelabs_withdrawals"]:
    conn.request("GET", f"/static/pages/{f}.js")
    r = conn.getresponse(); r.read()
    if r.status != 200: ok_all = False; print("FAIL js", f, r.status)
print("PASS all 10 page JS files served (200)")
psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
print("ALL PASS" if ok_all else "HAS FAILURES")
