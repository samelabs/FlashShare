import http.client, time, sys, os, re, json
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2, hmac, hashlib, secrets as _sec
from config import SESSION_SECRET, COOKIE_NAME

PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, a=()):
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

ts = int(time.time()); uname = f"uc{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'u','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+600, ts))

conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
ck = f"{COOKIE_NAME}={tok_s}; flashshare_lang=zh"
def get(p):
    conn.request("GET", p, headers={"Cookie": ck})
    r = conn.getresponse(); return r.status, r.read().decode()

# ① no raw keys in ANY rendered account page (5 langs)
PAGES = ["/account/profile", "/account/security", "/account/credits", "/account/tokens", "/account/tools", "/account/earnings", "/", "/agents"]
KEY_RE = re.compile(r">([a-z]+(?:_[a-z0-9]+){1,3})<")
KNOWN = ("mytools_", "earn_", "acct_", "menu_", "agents_", "cap_", "tool_", "credits_", "token", "profile", "err_", "zip_", "daily_")
leaks = []
for page in PAGES:
    for lang in ["zh", "en", "fr", "ar", "ja"]:
        conn.request("GET", page, headers={"Cookie": f"{COOKIE_NAME}={tok_s}; flashshare_lang={lang}"})
        html = conn.getresponse().read().decode()
        for m in KEY_RE.finditer(html):
            k = m.group(1)
            if k.startswith(KNOWN):
                leaks.append(f"{page}:{lang}:{k}")
c1 = not leaks
print(("PASS" if c1 else "FAIL"), f"1. zero raw-key leaks across {len(PAGES)} pages x5 langs" + (f" — {leaks[:4]}" if leaks else ""))

# ② tools page: list-first + primary create + empty state
st, thtml = get("/account/tools")
c2 = ('class="primary" id="openToolModal"' in thtml
      and 'id="toolList"' in thtml
      and 'id="toolNone"' in thtml
      and "还没有发布过工具" in thtml)
print(("PASS" if c2 else "FAIL"), "2. tools page: primary create btn + list + empty state")

# ③ tab strip refined, no sidebar, no duplicate user card
all_tb = True
for p in PAGES[:6]:
    st, h = get(p)
    ok = 'class="acct-nav"' in h and 'acct-tab active' in h and 'acct-side' not in h and 'acct-main' not in h
    if not ok: all_tb = False; print("   tab strip missing on", p)
css = open('/opt/flashshare/static/style.css').read()
c3 = all_tb and '.acct-tab.active' in css and '.acct-tab-sep' in css and 'acct-side' not in css
print(("PASS" if c3 else "FAIL"), "3. refined pill tabs on all 6 pages, no sidebar/no dup user card")

psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
ok = all([c1, c2, c3])
print("ALL PASS" if ok else "HAS FAILURES")
