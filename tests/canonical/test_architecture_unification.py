#!/usr/bin/env python3
"""Architecture unification canonical — one architecture, one protocol,
one data model, one secret format, PostgreSQL as the only app database.
No legacy fallback / dual read / dual write / migration-on-startup for
retired schemas. U1..U5 prove the unified architecture holds."""

import os as _os
import sys as _sys
sys = _sys

_TEST_PORT = _os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT is required.\n"); _sys.exit(2)
try:
    if int(_TEST_PORT, 10) == 8000:
        _sys.stderr.write("ERROR: production port not allowed.\n"); _sys.exit(2)
except ValueError:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT must be an integer.\n"); _sys.exit(2)
TEST_DB = _os.environ.get("TEST_DB_URL")
if not TEST_DB:
    _sys.stderr.write("ERROR: TEST_DB_URL is required.\n"); _sys.exit(2)

_REPO = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, _os.path.join(_REPO, "core"))
sys.path.insert(0, _os.path.join(_REPO, "app"))
_os.environ.setdefault("DATABASE_URL", TEST_DB.replace("postgresql://", "postgresql+psycopg://"))
_os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "canonical-test")

FAIL = []
COUNT = 0
def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:110]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

# ═══ U1: Vault single format ═══
import secrets_vault
from cryptography.fernet import Fernet
KEY = Fernet.generate_key()
_os.environ["TOOL_VAULT_KEY"] = KEY.decode()
import importlib
importlib.reload(secrets_vault)

s = secrets_vault.seal("topsecret")
check("U1 seal → enc1 format", s.startswith("enc1:"))
check("U1 unseal(enc1) → secret", secrets_vault.unseal(s) == "topsecret")
check("U1 unseal('') → ''", secrets_vault.unseal("") == "")

raised = None
try:
    secrets_vault.unseal("plaintext-secret")
except Exception as e:
    raised = e
check("U1 unseal(non-enc1 non-empty) → typed VaultDecryptError (fail closed)",
      isinstance(raised, secrets_vault.VaultDecryptError), raised)

# zero network is structural: unseal touches no transport module
_vsrc = open(_os.path.join(_REPO, "core", "secrets_vault.py"), encoding="utf-8").read()
check("U1 vault 无 network/transport 引用",
      "http" not in _vsrc.lower() and "socket" not in _vsrc.lower())

# ═══ U2: memberships 单一模型 ═══
import psycopg2
PG = TEST_DB.replace("postgresql+psycopg://", "postgresql://")
_con = psycopg2.connect(PG); _con.autocommit = True
_cur = _con.cursor()
_cur.execute("select to_regclass('memberships') is not null")
check("U2 memberships 表存在", _cur.fetchone()[0] is True)
try:
    _cur.execute("select to_regclass('user_plans')")
    check("U2 user_plans 表不存在 (fresh DB)", _cur.fetchone()[0] is None)
except Exception as exc:
    check("U2 user_plans 表不存在 (fresh DB)", False, exc)

_dbsrc = open(_os.path.join(_REPO, "core", "database.py"), encoding="utf-8").read()
check("U2 core runtime user_plans 零引用", "user_plans" not in _dbsrc)
check("U2 无 legacy:<user> membership fabrication",
      "'legacy:'" not in _dbsrc and '"legacy:' not in _dbsrc)
check("U2 无 startup migration-to-memberships SQL (INSERT INTO memberships ... SELECT FROM user_plans)",
      "INSERT INTO memberships" not in _dbsrc)

# ═══ U3: accept 唯一 executable input contract (真实 route) ═══
import time as _time
ts = int(_time.time())
_u = "au" + __import__("uuid").uuid4().hex[:7]
_cur.execute(
    "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
    " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id", (_u, f"{_u}@t.co", ts, ts))
_uid = _cur.fetchone()[0]

from app.main import app
from app.web_auth import create_browser_session, COOKIE_NAME
from fastapi.testclient import TestClient
client = TestClient(app)
sess = create_browser_session(_uid)

# resolver pin (无真 DNS): pub.example → public IP
import remote as _remote_mod
import socket as _socket
def _fake_resolve(host, port):
    if host == "pub.example":
        return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
    raise OSError("no such host in test")
_remote_mod._resolve_host = _fake_resolve

def _post(body):
    base = {"tool_id": body.pop("tool_id"), "label": "AU", "url": "https://pub.example/x",
            "format_families": "image"}
    base.update(body)
    return client.post("/api/usertools", json=base, cookies={COOKIE_NAME: sess})

r = _post({"tool_id": f"au1{_u}"})  # 无 accept
check("U3 missing accept → 422 bad_accept",
      r.status_code == 422 and r.json().get("error") == "bad_accept", f"{r.status_code} {r.text[:80]}")
r = _post({"tool_id": f"au2{_u}", "accept": None})
check("U3 accept=None → 422 bad_accept",
      r.status_code == 422 and r.json().get("error") == "bad_accept", f"{r.status_code} {r.text[:80]}")
r = _post({"tool_id": f"au3{_u}", "accept": []})
check("U3 accept=[] → 422 bad_accept",
      r.status_code == 422 and r.json().get("error") == "bad_accept", f"{r.status_code} {r.text[:80]}")
r = _post({"tool_id": f"au4{_u}", "accept": [".png"]})
check("U3 explicit valid accept → 200",
      r.status_code == 200 and r.json().get("ok"), f"{r.status_code} {r.text[:160]}")
r = _post({"tool_id": f"au5{_u}", "accept": ["application/pdf"]})  # MIME 不再解释
check("U3 MIME token (非 canonical ext) → 422 bad_accept",
      r.status_code == 422 and r.json().get("error") == "bad_accept", f"{r.status_code} {r.text[:80]}")
r = _post({"tool_id": f"au6{_u}", "accept": [".exe"]})  # 出族
check("U3 out-of-family ext → 422 bad_accept",
      r.status_code == 422 and r.json().get("error") == "bad_accept", f"{r.status_code} {r.text[:80]}")

# ═══ U4: PostgreSQL-only app ═══
_cfg = open(_os.path.join(_REPO, "core", "config.py"), encoding="utf-8").read()
check("U4 config.py 无 DB_PATH", "DB_PATH" not in _cfg)
check("U4 config.py 无 FLASHSHARE_DB", "FLASHSHARE_DB" not in _cfg)
check("U4 core/database.py 无 flashshare.sqlite", "flashshare.sqlite" not in _dbsrc)
import os
check("U4 scripts/migrate_sqlite_to_pg.py 不存在",
      not os.path.exists(_os.path.join(_REPO, "scripts", "migrate_sqlite_to_pg.py")))
check("U4 setup.sh 不存在", not os.path.exists(_os.path.join(_REPO, "setup.sh")))
# Gokapi 自身 sqlite 是第三方组件 — 允许 (backup.sh 的 gokapi.sqlite 引用)
_bk = open(_os.path.join(_REPO, "scripts", "backup.sh"), encoding="utf-8").read()
check("U4 backup.sh 仅引用 Gokapi sqlite (第三方豁免)",
      "gokapi.sqlite" in _bk and "flashshare.sqlite" not in _bk)

# ═══ U5: setup authority ═══
check("U5 setup.sh 已删除 (无 SQLite bootstrap 入口)",
      not os.path.exists(_os.path.join(_REPO, "setup.sh")))

# ═══ M: backup filename consistency (static) ═══
check("M backup.sh 无 gokapi-$ST typo (weekly 与 daily 文件名一致)",
      "gokapi-$ST." not in _bk and _bk.count("gokapi-$STAMP.tar.gz") >= 4)
check("M backup.sh set -euo pipefail", "set -euo pipefail" in _bk)

# 清场
_cur.execute("delete from pricing where tool_id like %s", (f"{_u}/%",))
_cur.execute("delete from platform_tools where tool_key like %s", (f"{_u}/%",))
_cur.execute("delete from sessions where user_id=%s", (_uid,))
_cur.execute("delete from users where id=%s", (_uid,))
_con.close()

print()
if FAIL:
    print(f"FAIL: {len(FAIL)} — " + "; ".join(FAIL)); _sys.exit(1)
print(f"FAIL: 无 — 全绿 ({COUNT} checks)")
