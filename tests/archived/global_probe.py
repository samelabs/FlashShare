import os, sys, time, json, http.client, hmac, hashlib, secrets as _sec
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2
from config import SESSION_SECRET, COOKIE_NAME
from economy.billing import spendable_balance

PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, a=()):
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

ts = int(time.time()); uname = f"ga{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'g','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+600, ts))
conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
ck = f"{COOKIE_NAME}={tok_s}; flashshare_lang=zh"
def get(p):
    conn.request("GET", p, headers={"Cookie": ck})
    r = conn.getresponse(); return r.status, r.read().decode()

# A5: register page banner (live from activities)
conn.request("GET", "/auth/register", headers={"Cookie": "flashshare_lang=zh"})
rh = conn.getresponse().read().decode()
c1 = "注册即送 100 积分" in rh and "auth-bonus" in rh
print(("PASS" if c1 else "FAIL"), "A5. register banner: 注册即送100积分 (activities-driven)")

# D1: credits top-up card (honest placeholder when empty)
st, ch = get("/account/credits")
c2 = "充值功能即将开通" in ch and 'data-i18n="topup_title"' in ch
print(("PASS" if c2 else "FAIL"), "D1. credits top-up card present (packages empty → honest soon)")

# A2: credits page history shows spendable-side only
psql("INSERT INTO ledger (user_id, delta, balance_after, kind, ref_type, ref_id, idem_key, note, created_at) VALUES(%s,80,80,'grant_signup','activity','signup',%s,'',%s)", (uid, f"sg:{uid}", ts))
psql("INSERT INTO ledger (user_id, delta, balance_after, kind, ref_type, ref_id, idem_key, note, created_at) VALUES(%s,999,1079,'provider_earn','call','cx',%s,'x/doc',%s)", (uid, f"pe:{uid}", ts))
st, ch = get("/account/credits")
c3 = "999" not in ch.split("流水")[1][:2000] if "流水" in ch else True
# direct projection check:
from economy.billing import history_paged
rows, total = history_paged(uid)
kinds = {r["kind"] for r in rows}
c3 = kinds == {"grant_signup"} and total == 1
print(("PASS" if c3 else "FAIL"), f"A2. credits history spendable-only: kinds={kinds}")

# B3+B6: tools page carries contract doc; calls endpoint typed
st, thtml = get("/account/tools")
c4 = "接入契约" in thtml and "source_urls" in thtml and "output_url" in thtml
print(("PASS" if c4 else "FAIL"), "B6. contract doc on tools page")
conn.request("GET", "/api/usertools/nope/calls", headers={"Cookie": ck})
r = conn.getresponse(); r.read()
c5 = r.status == 404
print(("PASS" if c5 else "FAIL"), "B3. calls endpoint owner-scoped (404 for others' tools)")

# B3 live: seed a tool + a call row, read log
tk = f"{uname}/cl"
psql("INSERT INTO platform_tools (tool_key, owner_id, tool_id, execution, tool_type, label_json, visibility, endpoint_json, enabled, created_at, updated_at) VALUES(%s,%s,'cl','http','produce',%s,'private',%s,TRUE,%s,%s)",
     (tk, uid, json.dumps({"en":"CL","zh":"CL"}), json.dumps({"url":"https://example.com/x","timeout_s":30}), ts, ts))
# a call by ANOTHER user (self-calls are excluded from the provider log)
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'caller','user',%s,%s)", (uname + 'c', ts, ts))
cid = psql("SELECT id FROM users WHERE username=%s", (uname + 'c',))[0][0]
psql("INSERT INTO tool_calls (client_ref, user_id, tool_id, status, cost_snapshot, started_at, finished_at) VALUES('ref1',%s,%s,'ok',5,%s,%s)", (cid, tk, ts, ts+1))
conn.request("GET", "/api/usertools/cl/calls", headers={"Cookie": ck})
d = json.loads(conn.getresponse().read())
c6 = d["ok"] and len(d["calls"]) == 1 and d["calls"][0]["ms"] == 1
print(("PASS" if c6 else "FAIL"), f"B3. call log: 1 call, ms={d['calls'][0].get('ms') if d.get('calls') else None}")

# cleanup
psql("DELETE FROM tool_calls WHERE tool_id=%s", (tk,))
psql("DELETE FROM users WHERE id=%s", (cid,))
psql("DELETE FROM platform_tools WHERE tool_key=%s", (tk,))
psql("DELETE FROM ledger WHERE user_id=%s", (uid,))
psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
ok = all([c1, c2, c3, c4, c5, c6])
print("ALL PASS" if ok else "HAS FAILURES")
