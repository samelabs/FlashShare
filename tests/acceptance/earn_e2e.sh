#!/bin/sh
# earnings loop E2E: earn → withdraw request → approve deducts → reject keeps → idempotency
set -e
set -a; . /etc/flashshare.env; set +a
/usr/bin/python3 - <<'PYEOF'
import http.client, json, time, sys, os
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2
PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")

def psql(q, *a):
    if len(a) == 1 and isinstance(a[0], tuple): a = a[0]
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

# provider with earned credits (ledger provider_earn rows)
ts = int(time.time())
uname = f"ern{ts%100000}"
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'e','user',%s,%s)", uname, ts, ts)
uid = psql("SELECT id FROM users WHERE username=%s", uname)[0][0]
psql("INSERT INTO ledger(user_id, delta, balance_after, kind, idem_key, created_at) VALUES(%s, 50000, 50000, 'provider_earn', %s, %s)",
     uid, f"earn:{uid}:t", ts)

from earnings import earnings_summary, request_withdrawal, process_withdrawal, MIN_WITHDRAWAL
s = earnings_summary(uid)
assert s == {"earned": 50000, "withdrawn": 0, "pending": 0, "available": 50000}, s
print("1. summary earned=50000 available=50000 OK")

# min withdrawal enforced
try:
    request_withdrawal(uid, MIN_WITHDRAWAL - 1); raise SystemExit("min FAIL")
except ValueError as e:
    assert str(e) == "withdrawal_min"
print("2. min withdrawal enforced OK")

# over-available rejected
try:
    request_withdrawal(uid, 50001); raise SystemExit("avail FAIL")
except ValueError as e:
    assert str(e) == "withdrawal_insufficient"
print("3. over-available rejected OK")

# request → pending reduces available
w1 = request_withdrawal(uid, 20000)
assert w1["status"] == "pending"
s = earnings_summary(uid)
assert s["pending"] == 20000 and s["available"] == 30000, s
print("4. request → pending 20000, available 30000 OK")

# over-available now (pending included) rejected
try:
    request_withdrawal(uid, 30001); raise SystemExit("pending FAIL")
except ValueError as e:
    assert str(e) == "withdrawal_insufficient"
print("5. pending counts against available OK")

# approve → ledger withdraw row + balance drop, one tx
r1 = process_withdrawal(w1["id"], True, admin_user_id=2)
assert r1["status"] == "approved"
bal = psql("SELECT COALESCE(SUM(delta),0) FROM ledger WHERE user_id=%s", uid)[0][0]
assert bal == 30000, bal
s = earnings_summary(uid)
assert s["withdrawn"] == 20000 and s["available"] == 30000, s
print("6. approve: ledger -20000 (balance 30000), withdrawn=20000 OK")

# double-process rejected (idempotent by status)
try:
    process_withdrawal(w1["id"], True); raise SystemExit("double FAIL")
except ValueError as e:
    assert str(e) == "already_processed"
bal = psql("SELECT COALESCE(SUM(delta),0) FROM ledger WHERE user_id=%s", uid)[0][0]
assert bal == 30000
print("7. double-approve impossible, balance intact OK")

# reject path: request + reject → no ledger change
w2 = request_withdrawal(uid, 20000)
r2 = process_withdrawal(w2["id"], False, admin_user_id=2)
assert r2["status"] == "rejected"
bal = psql("SELECT COALESCE(SUM(delta),0) FROM ledger WHERE user_id=%s", uid)[0][0]
assert bal == 30000
s = earnings_summary(uid)
assert s["pending"] == 0 and s["available"] == 30000
print("8. reject: credits untouched, pending released OK")

# API surface: earnings endpoint shape (session auth)
import hmac, hashlib, secrets as _sec
from config import SESSION_SECRET, COOKIE_NAME
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", th, uid, ts+600, ts)
conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
conn.request("GET", "/api/earnings", headers={"Cookie": f"{COOKIE_NAME}={tok_s}"})
d = json.loads(conn.getresponse().read())
assert d["ok"] and d["summary"]["available"] == 30000 and d["min_withdrawal"] == MIN_WITHDRAWAL, d
assert all("url" not in t for t in d["tools"])
print("9. /api/earnings: summary + breakdown OK")

# HTTP withdraw endpoint validation
conn.request("POST", "/api/earnings/withdraw", body=json.dumps({"amount": 50}),
             headers={"Cookie": f"{COOKIE_NAME}={tok_s}", "Content-Type": "application/json"})
d = json.loads(conn.getresponse().read())
assert not d["ok"] and d["error"] == "withdrawal_min", d
print("10. HTTP withdraw min-rejection OK")

# samelabs page gate: non-admin 403
conn.request("GET", "/samelabs/withdrawals", headers={"Cookie": f"{COOKIE_NAME}={tok_s}", "Follow": "0"})
st = conn.getresponse().status
assert st == 302, st  # _gate redirects non-operators to login (site-wide semantics)
print("11. /samelabs/withdrawals gated (302 non-admin) OK")

# cleanup
psql("DELETE FROM withdrawals WHERE user_id=%s", uid)
psql("DELETE FROM ledger WHERE user_id=%s", uid)
psql("DELETE FROM sessions WHERE user_id=%s", uid)
psql("DELETE FROM users WHERE id=%s", uid)
print("ALL EARNINGS LOOP E2E PASSED")
PYEOF
