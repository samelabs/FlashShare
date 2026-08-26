import os, sys, time, json, http.client, hmac, hashlib, secrets as _sec
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2
from config import SESSION_SECRET, COOKIE_NAME

PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, a=()):
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

ts = int(time.time()); uname = f"ex{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'e','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+600, ts))

# seed an upload row ALREADY EXPIRED but pointing at a "live" gokapi id
# (simulate the drift window the audit found)
pid = f"exp{ts%1000000}"
psql("INSERT INTO uploads(user_id, ip, gokapi_id, public_id, file_name, content_type, size_bytes, expiry_days, allowed_downloads, expire_at, url_download, url_hotlink, created_at) VALUES(%s,'127.0.0.1','fakegok123',%s,'t.png','image/png',100,1,10,%s,%s,%s,%s)",
     (uid, pid, ts - 100, f"https://518.plus/f/{pid}", f"https://gokapi.internal/h/fakegok123", ts))

conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
# 8.3: detail page must 404 for an expired row
conn.request("GET", f"/f/{pid}", headers={"Cookie": f"{COOKIE_NAME}={tok_s}"})
r1 = conn.getresponse(); body1 = r1.read().decode()
c1 = r1.status == 404
print(("PASS" if c1 else "FAIL"), f"8.3a detail page expired row → 404 (got {r1.status})")

# 8.3b: download must 404 too (was: direct gokapi hotlink hop)
conn.request("GET", f"/f/{pid}?download=1", headers={"Cookie": f"{COOKIE_NAME}={tok_s}"})
r2 = conn.getresponse(); r2.read()
c2 = r2.status == 404
print(("PASS" if c2 else "FAIL"), f"8.3b download expired row → 404 (got {r2.status})")

# 8.4: purge is boot-time; verify no expired rows remain right now
n = psql("SELECT COUNT(*) FROM uploads WHERE expire_at < EXTRACT(EPOCH FROM NOW()) AND public_id != %s", (pid,))[0][0]
c3 = n == 0
print(("PASS" if c3 else "FAIL"), f"8.4 expired rows in table = {n}")

psql("DELETE FROM uploads WHERE public_id=%s", (pid,))
psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
ok = all([c1, c2, c3])
print("ALL PASS" if ok else "HAS FAILURES")
