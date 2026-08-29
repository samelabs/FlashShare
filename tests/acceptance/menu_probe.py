import http.client, time, sys, os, re
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2, hmac, hashlib, secrets as _sec
from config import SESSION_SECRET, COOKIE_NAME

PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")

def psql(q, *a):
    if len(a) == 1 and isinstance(a[0], tuple):
        a = a[0]
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

ts = int(time.time())
uname = f"mn{ts % 100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'menu t','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts + 600, ts))

conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
conn.request("GET", "/", headers={"Cookie": f"{COOKIE_NAME}={tok_s}; flashshare_lang=zh"})
html = conn.getresponse().read().decode()

sec_count = html.count('class="menu-sec"')
checks = {
    "sections >= 4 (money/recent/nav/logout)": sec_count >= 4,
    "credits row right-aligned value": 'class="menu-val" id="headerCredits"' in html,
    "credits label i18n": ">积分余额<" in html and 'data-i18n="credits_balance"' in html,
    "earnings entry": 'href="/account/tools"' in html and ">我的收益<" in html,
    "publish entry": ">发布工具<" in html,
    "tokens entry": 'href="/account/tokens"' in html and ">API 令牌<" in html,
    "mcp entry": 'href="/agents"' in html and ">MCP 接入<" in html,
    "recent row (left-aligned like others)": 'id="historyBtn"' in html and ">最近记录<" in html,
    "settings row": 'href="/account/profile"' in html and ">设置<" in html,
    "logout row": "logout-link" in html,
    "every row has icon svg": len(re.findall(r'<a href="/account/[a-z]+" role="menuitem">\s*<svg', html)) >= 4,
    "no legacy classes": "dropdown-history" not in html and "dropdown-credits" not in html,
}
for k, v in checks.items():
    print(("PASS" if v else "FAIL"), k)

css = open("/opt/flashshare/static/518.css").read()
checks["css: no centering (text-align left)"] = ".user-dropdown a, .user-dropdown button" in css and "margin-left: auto" in css.split(".menu-val")[1][:120]
checks["css: sections divided"] = ".menu-sec + .menu-sec" in css

for k, v in checks.items():
    if not v: print("FAIL", k)
psql("DELETE FROM api_tokens WHERE user_id=%s", (uid,))
psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
print("ALL PASS" if all(checks.values()) else "HAS FAILURES")
