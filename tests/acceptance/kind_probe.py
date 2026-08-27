import http.client, time, sys, os, hmac, hashlib, secrets as _sec
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2
from config import SESSION_SECRET, COOKIE_NAME
PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, a=()):
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows
ts = int(time.time()); uname = f"kp{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'k','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO ledger (user_id, delta, balance_after, kind, ref_type, ref_id, idem_key, note, created_at) VALUES(%s,100,100,'grant_signup','activity','signup',%s,'',%s)", (uid, f"sg:{uid}", ts))
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+600, ts))
conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
ok_all = True
for lang, want in [("zh", "注册赠送"), ("en", "Signup bonus"), ("fr", "Bonus d'inscription"), ("ja", "登録ボーナス")]:
    conn.request("GET", "/account/credits", headers={"Cookie": f"{COOKIE_NAME}={tok_s}; flashshare_lang={lang}"})
    h = conn.getresponse().read().decode()
    ok = want in h and ">grant_signup<" not in h
    print(("PASS" if ok else "FAIL"), f"{lang}: '{want}' rendered, no raw kind")
    if not ok: ok_all = False
psql("DELETE FROM ledger WHERE user_id=%s", (uid,))
psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
print("ALL PASS" if ok_all else "HAS FAILURES")
