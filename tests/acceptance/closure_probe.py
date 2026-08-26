import http.client, time, sys, os, json, hmac, hashlib
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2, secrets as _sec
from config import SESSION_SECRET, COOKIE_NAME

PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, *a):
    if len(a) == 1 and isinstance(a[0], tuple): a = a[0]
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

ts = int(time.time())
uname = f"cl{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at, payout_channel) VALUES(%s,'closure t','user',%s,%s,%s)",
     (uname, ts, ts, "USDT-TRC20:TQn9Y2khEsLJvCjKq9gY..."))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+600, ts))

# seed revenue: provider_earn line (150 earned) so withdrawal can be tested
psql("INSERT INTO ledger (user_id, delta, balance_after, kind, ref_type, ref_id, idem_key, note, created_at) VALUES(%s,150,150,'provider_earn','call','probe_call_1','payout:probe_call_1',%s,%s)",
     (uid, f"{uname}/demo", ts))

conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
ck = f"{COOKIE_NAME}={tok_s}; flashshare_lang=zh"
def req(method, path, body=None, ctype="application/json"):
    headers = {"Cookie": ck}
    if body is not None: headers["Content-Type"] = ctype
    conn.request(method, path, body=json.dumps(body) if body is not None and ctype=="application/json" else body, headers=headers)
    r = conn.getresponse(); raw = r.read()
    try: return r.status, json.loads(raw)
    except Exception: return r.status, raw.decode()

# 1. /api/earnings carries revenue_lines
st, d = req("GET", "/api/earnings")
c1 = st == 200 and d["ok"] and d["summary"]["earned"] == 150 and len(d["revenue_lines"]) == 1 and d["revenue_lines"][0]["tool"] == f"{uname}/demo"
print(("PASS" if c1 else "FAIL"), "1. revenue_lines projection (tool/delta)")

# 2. earnings page renders 明细 section + 充值 link
conn.request("GET", "/account/earnings", headers={"Cookie": ck})
html = conn.getresponse().read().decode()
c2 = 'id="revLines"' in html and "收益明细" in html and "充值" in html and 'href="/account/credits"' in html
print(("PASS" if c2 else "FAIL"), "2. earnings page: detail section + top-up entry")

# 3. withdraw request → queue row carries channel
st, d = req("POST", "/api/earnings/withdraw", {"amount": 100, "note": "probe"})
c3 = st == 200 and d["ok"]
print(("PASS" if c3 else "FAIL"), "3. withdrawal request accepted")
wid = d.get("id")
row = psql("SELECT w.status, u.payout_channel FROM withdrawals w JOIN users u ON u.id=w.user_id WHERE w.id=%s", (wid,))[0]
c3b = row[0] == "pending" and "USDT" in row[1]
print(("PASS" if c3b else "FAIL"), f"3b. queue row shows channel ({row[1][:20]}…)")

# 4. profile page: 收款方式 field
conn.request("GET", "/account/profile", headers={"Cookie": ck})
ph = conn.getresponse().read().decode()
c4 = 'name="payout_channel"' in ph and "收款方式" in ph and "USDT-TRC20" in ph
print(("PASS" if c4 else "FAIL"), "4. profile: payout field with saved value")

# 5. operator queue page: channel + txn_ref columns
aid = psql("SELECT id FROM users WHERE username=%s", ("ainotebook",))[0][0]
ats = int(time.time())
atok = _sec.token_urlsafe(32)
ath = hmac.new(SESSION_SECRET.encode(), atok.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (ath, aid, ats+600, ats))
conn.request("GET", "/samelabs/withdrawals", headers={"Cookie": f"{COOKIE_NAME}={atok}"})
wh = conn.getresponse().read().decode()
c5 = "收款渠道" in wh and "凭证" in wh and "USDT" in wh
print(("PASS" if c5 else "FAIL"), "5. operator queue: channel + ref columns live")

# 6. approve with txn_ref → recorded + ledger consumed
conn.request("POST", f"/samelabs/withdrawals/{wid}/process", body=json.dumps({"approve": True, "txn_ref": "TX-77-PROBE"}),
             headers={"Cookie": f"{COOKIE_NAME}={atok}", "Content-Type": "application/json"})
r6 = conn.getresponse(); d = json.loads(r6.read()); st = r6.status
c6 = st == 200 and d.get("ok") and d.get("status") == "approved"
print(("PASS" if c6 else "FAIL"), "6. approve with txn_ref")
row = psql("SELECT txn_ref, status FROM withdrawals WHERE id=%s", (wid,))[0]
c6b = row[0] == "TX-77-PROBE" and row[1] == "approved"
print(("PASS" if c6b else "FAIL"), f"6b. txn_ref persisted ({row[0]})")
bal = psql("SELECT COALESCE(SUM(delta),0) FROM ledger WHERE user_id=%s", (uid,))[0][0]
c6c = bal == 50  # 150 - 100
print(("PASS" if c6c else "FAIL"), f"6c. ledger net 150-100=50 (got {bal})")

# cleanup
psql("DELETE FROM ledger WHERE user_id=%s", (uid,))
psql("DELETE FROM withdrawals WHERE user_id=%s", (uid,))
psql("DELETE FROM sessions WHERE user_id IN (%s,%s)", (uid, aid))
psql("DELETE FROM users WHERE id=%s", (uid,))
ok = all([c1, c2, c3, c3b, c4, c5, c6, c6b, c6c])
print("ALL PASS" if ok else "HAS FAILURES")
