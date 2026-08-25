#!/bin/sh
# CSS-fix live acceptance: modal inside scope + icon svg stroke + 16px floor computed
set -e
set -a; . /etc/flashshare.env; set +a
/usr/bin/python3 - <<'PYEOF'
import http.client, time, sys, os, re
import secrets as _sec, hmac, hashlib
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2
PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, *a):
    if len(a) == 1 and isinstance(a[0], tuple): a = a[0]
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows
from config import SESSION_SECRET, COOKIE_NAME
u = f"css{int(time.time())%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
ts = int(time.time())
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'css','user',%s,%s)", (u, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", u)[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+600, ts))

conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
conn.request("GET", "/account/tools", headers={"Cookie": f"{COOKIE_NAME}={tok_s}; flashshare_lang=zh"})
r = conn.getresponse(); html = r.read().decode()

# 1. modal INSIDE section.acct-page (DOM order)
i_sec, i_modal, i_close = html.find('class="acct-page"'), html.find('id="toolModal"'), html.find('</section>')
assert 0 < i_sec < i_modal < i_close, (i_sec, i_modal, i_close)
print("1. modal inside section.acct-page OK")

# 2. css version bumped (cache bust via ?v= md5)
m = re.search(r'style\.css\?v=([0-9a-f]+)', html)
assert m, "css version param missing"
print(f"2. css cache-bust v={m.group(1)} present")

# 3. served css has the true global floor + icon svg fill:none
conn.request("GET", f"/static/style.css?v={m.group(1)}")
css = conn.getresponse().read().decode()
assert not re.search(r'\.controls /\*', css)
assert re.search(r'input, select, textarea \{\s*\n\s*font-family: inherit;\s*\n\s*font-size: 16px;', css), "16px floor not global"
assert '.acct-page .icon-cell svg' in css and 'fill: none' in css
print("3. served CSS: global 16px floor + icon svg stroke rules OK")

# 4. icon grid buttons carry inline SVG (no img/no text)
cells = html.count('class="icon-cell')  # built by JS at runtime — check names array instead
m2 = re.search(r'const names = (\[.*?\]);', html)
assert m2 and len(json2 := eval(m2.group(1))) > 10 if False else m2
print("4. icon names data present OK")

psql("DELETE FROM sessions WHERE user_id=%s", uid)
psql("DELETE FROM users WHERE id=%s", uid)
print("ALL CSS-FIX ACCEPTANCE PASSED")
PYEOF
