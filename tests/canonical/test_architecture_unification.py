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

# U2b: startup schema 只有 current — 零 ALTER/DROP/RENAME/backfill
check("U2b database.py 无 ALTER TABLE", "ALTER TABLE" not in _dbsrc)
check("U2b 无 DROP COLUMN", "DROP COLUMN" not in _dbsrc)
check("U2b 无 RENAME COLUMN", "RENAME COLUMN" not in _dbsrc)
check("U2b 无 DROP INDEX (旧 index 清理)", "DROP INDEX" not in _dbsrc)
check("U2b 无 creem_price_id rename migration", "creem_price_id" not in _dbsrc)
check("U2b 无 presets_json migration", "presets_json" not in _dbsrc)
check("U2b 无 historical UPDATE backfill (plans)", "UPDATE plans" not in _dbsrc)

# U2c: fresh schema 的 current 列直接由 CREATE TABLE 获得
def _cols(table, names):
    _cur.execute(
        "select column_name from information_schema.columns "
        "where table_name=%s", (table,))
    have = {r[0] for r in _cur.fetchall()}
    return all(n in have for n in names), sorted(set(names) - have)

ok, miss = _cols("users", ["payout_channel"])
check("U2c users.payout_channel 在 CREATE TABLE", ok, miss)
ok, miss = _cols("plans", ["period_days", "period", "badge_key",
                           "ingress_files_24h", "ingress_bytes_24h",
                           "expiry_days", "download_limits", "creem_product_id"])
check("U2c plans entitlement/badge/period 列齐", ok, miss)
ok, miss = _cols("platform_tools", ["desc_json", "sort_order", "featured",
                                    "pass_through", "icon"])
check("U2c platform_tools declaration 列齐", ok, miss)
_cur.execute("select column_default from information_schema.columns "
             "where table_name='platform_tools' and column_name='icon'")
check("U2c platform_tools.icon DEFAULT 'wand'", _cur.fetchone()[0] is not None)
check("U2c fresh 库无 presets_json 列", not _cols("platform_tools", ["presets_json"])[0])
_cur.execute("select indexname from pg_indexes where tablename='tool_calls'")
_idx = {r[0] for r in _cur.fetchall()}
check("U2c tool_calls 当前 unique live idx 存在", "tool_calls_client_ref_live_idx" in _idx)
check("U2c 无旧 tool_calls index", not ({"tool_calls_client_ref_ok_idx",
                                         "tool_calls_client_ref_idx"} & _idx), _idx)

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

# ═══ U3b: stored accept semantics (extensions_for_accept 单点) ═══
from governance.formats import extensions_for_accept as _efa
from governance.formats import VOCAB_EXTS
check("U3b accept=None → None (explicit any)", _efa(None) is None)
try:
    _efa([]); check("U3b accept=[] rejected", False, "no raise")
except ValueError:
    check("U3b accept=[] rejected", True)
try:
    _efa(["application/pdf"]); check("U3b MIME token rejected", False, "no raise")
except KeyError:
    check("U3b MIME token rejected", True)
try:
    _efa(".pdf"); check("U3b bare string rejected", False, "no raise")
except TypeError:
    check("U3b bare string rejected", True)
try:
    _efa([".exe"]); check("U3b unknown ext rejected", False, "no raise")
except KeyError:
    check("U3b unknown ext rejected", True)
check("U3b canonical list → set", _efa([".png", ".jpg"]) == {".png", ".jpg"})

# ═══ U3d: stored physical shapes — parse_stored_accept 单点 ═══
from governance.formats import parse_stored_accept as _psa
check("U3d stored SQL None → None (唯一 any)", _psa(None) is None)
for _bad, _why in [("", "empty string"), ('null', "JSON null"),
                   ("[]", "empty array"), ('{".png": 1}', "JSON object"),
                   ('[".png", "image/png"]', "MIME array"),
                   ('[".exe"]', "unknown ext"), ('[""]', "empty token"),
                   ('".png"', "JSON scalar string"), ("not json", "invalid JSON")]:
    try:
        _psa(_bad); check(f"U3d stored {_why} → invalid", False, "no raise")
    except (KeyError, TypeError, ValueError):
        check(f"U3d stored {_why} → invalid", True)
check("U3d stored canonical ext list → set",
      _psa('[".png", ".jpg"]') == {".png", ".jpg"})

# ═══ U3e: extensions_for_accept 严格 list-only ═══
try:
    _efa((".png",)); check("U3e tuple → TypeError", False, "no raise")
except TypeError:
    check("U3e tuple → TypeError", True)
try:
    _efa({".png"}); check("U3e set → TypeError", False, "no raise")
except TypeError:
    check("U3e set → TypeError", True)
try:
    _efa({".png": 1}); check("U3e dict → TypeError", False, "no raise")
except TypeError:
    check("U3e dict → TypeError", True)

# ═══ U6: compatibility mechanisms absent ═══
_fm = open(_os.path.join(_REPO, "core", "governance", "formats.py"), encoding="utf-8").read()
check("U6 validate_family_keys absent", "def validate_family_keys" not in _fm)
_fm_out = __import__("importlib").import_module("governance.formats").format_manifest()
check("U6 format_manifest 无 mime_exts", "mime_exts" not in _fm_out)
check("U6 format_manifest families 无 mimes",
      all("mimes" not in v for v in _fm_out["families"].values()))
_pj = open(_os.path.join(_REPO, "static", "pages", "publish.js"), encoding="utf-8").read()
check("U6 publish.js 无 MIME accept conversion (mime_exts/mimes 零引用)",
      "mime_exts" not in _pj and ".mimes" not in _pj)
_cat = open(_os.path.join(_REPO, "core", "governance", "catalog.py"), encoding="utf-8").read()
check("U6 catalog invalid accept 不原样返回 (无 token-preserve fallback)",
      "sorted(str(t) for t in tokens" not in _cat)
_earn = open(_os.path.join(_REPO, "core", "economy", "earnings.py"), encoding="utf-8").read()
check("U6 earnings.ensure_schema absent", "def ensure_schema" not in _earn)
_main = open(_os.path.join(_REPO, "app", "main.py"), encoding="utf-8").read()
check("U6 main startup 无 ensure_schema call", "ensure_schema" not in _main)
check("U6 main startup 有 validate_catalog_rows", "validate_catalog_rows()" in _main)

# ═══ U3c: dispatch invalid stored accept → tool_unavailable (fail closed,
# pre-metering: no tool_call row, no charge, no network) ═══
_cur.execute("delete from pricing where tool_id='badaccept'")
_cur.execute("delete from platform_tools where tool_key='badaccept'")
_cur.execute(
    "insert into platform_tools (tool_key, owner_id, tool_id, execution, tool_type,"
    " label_json, icon, accept_json, multi, visibility, enabled, trusted,"
    " created_at, updated_at) values (%s, NULL, %s, 'local', 'produce', '{}',"
    " 'wand', '[\"application/pdf\"]', false, 'public', true, false, %s, %s)",
    ("badaccept", "badaccept", ts, ts))
for _bk, _bj in (("badaccept2", "''"), ("badaccept3", "'null'")):
    _cur.execute("delete from pricing where tool_id=%s", (_bk,))
    _cur.execute("delete from platform_tools where tool_key=%s", (_bk,))
    _cur.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution, tool_type,"
        " label_json, icon, accept_json, multi, visibility, enabled, trusted,"
        " created_at, updated_at) values (%s, NULL, %s, 'local', 'produce', '{}',"
        " 'wand', %s, false, 'public', true, false, %s, %s)",
        (_bk, _bk, _bj, ts, ts))
_cur.execute(
    "insert into pricing (tool_id, cost, enabled, updated_at) values (%s, 5, true, %s)",
    ("badaccept", ts))
_cur.execute("select coalesce(sum(delta),0) from ledger where user_id=%s", (_uid,))
_l0 = _cur.fetchone()[0]
_cur.execute("select count(*) from tool_calls where user_id=%s", (_uid,))
_tc0 = _cur.fetchone()[0]
from identity.apitokens import create_token as _mk_tok
_vtok = _mk_tok(_uid, "au-test")["token"]  # Bearer path (v1 requires client_ref)
r = client.post("/api/v1/execute",
                json={"tool_id": "badaccept",
                      "source": {"files": [{"public_id": "x" * 12, "source_token": "t"}]},
                      "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 1},
                      "client_ref": "au-ref-1"},
                headers={"Authorization": f"Bearer {_vtok}"})
_ok_status = r.status_code in (503, 404, 422)
check("U3c invalid stored accept → tool_unavailable (pre-metering)",
      _ok_status and r.json().get("error") == "tool_unavailable",
      f"{r.status_code} {r.text[:80]}")
for _bk in ("badaccept2", "badaccept3"):
    r2 = client.post("/api/v1/execute",
                     json={"tool_id": _bk,
                           "source": {"files": [{"public_id": "x" * 12, "source_token": "t"}]},
                           "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 1},
                           "client_ref": f"au-ref-{_bk}"},
                     headers={"Authorization": f"Bearer {_vtok}"})
    check(f"U3c stored {_bk} (''/'null') → pre-metering fail",
          r2.status_code == 503 and r2.json().get("error") == "tool_unavailable",
          f"{r2.status_code} {r2.text[:80]}")
_cur.execute("select coalesce(sum(delta),0) from ledger where user_id=%s", (_uid,))
_l1 = _cur.fetchone()[0]
_cur.execute("select count(*) from tool_calls where user_id=%s", (_uid,))
_tc1 = _cur.fetchone()[0]
check("U3c ledger unchanged", _l1 == _l0, (_l0, _l1))
check("U3c tool_calls unchanged", _tc1 == _tc0, (_tc0, _tc1))

# ═══ U9: credential channel boundaries (真实 HTTP 边界) ═══
# Operator authority stays users.status; the CHANNEL is what closes:
# /samelabs accepts browser sessions only, ?key= query auth is gone.
_cur.execute("update users set status='samelabs' where id=%s", (_uid,))
r = client.get("/samelabs", cookies={COOKIE_NAME: sess}, follow_redirects=False)
check("U9 operator session → /samelabs allowed",
      r.status_code == 200, f"{r.status_code} {r.text[:80]}")
r = client.get("/samelabs", headers={"Authorization": f"Bearer {_vtok}"},
               follow_redirects=False)
check("U9 operator bearer token → /samelabs denied (redirect)",
      r.status_code == 302 and "/auth/login" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location', '')}")
r = client.post("/samelabs/tools/x/pricing",
                headers={"Authorization": f"Bearer {_vtok}"},
                follow_redirects=False)
check("U9 operator bearer token → /samelabs POST denied (redirect)",
      r.status_code == 302 and "/auth/login" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location', '')}")
r = client.get("/samelabs?key=" + _vtok, follow_redirects=False)
check("U9 operator token via ?key= → /samelabs denied (query auth absent)",
      r.status_code == 302 and "/auth/login" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location', '')}")
_cur.execute("update users set status='active' where id=%s", (_uid,))
r = client.get("/samelabs", cookies={COOKIE_NAME: sess}, follow_redirects=False)
check("U9 normal browser session → /samelabs denied (redirect)",
      r.status_code == 302 and "/auth/login" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location', '')}")
r = client.get("/samelabs", follow_redirects=False)
check("U9 anonymous → /samelabs denied (redirect)",
      r.status_code == 302 and "/auth/login" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location', '')}")
# query-token channel is structurally absent from the auth resolver
_main_src = open(_os.path.join(_REPO, "app", "main.py"), encoding="utf-8").read()
check("U9 main.py 无 query key 认证分支",
      'query_params.get("key"' not in _main_src)
check("U9 agents.html 无 ?key= 直连示例",
      "?key=" not in open(_os.path.join(_REPO, "app", "templates", "agents.html"),
                          encoding="utf-8").read())
# Bearer + session keep working on the capability surface
r = client.get("/api/v1/me", headers={"Authorization": f"Bearer {_vtok}"})
check("U9 Bearer 在能力面 (v1 /me) 仍有效",
      r.status_code == 200 and r.json().get("user", {}).get("username") == _u,
      f"{r.status_code} {r.text[:80]}")
r = client.get("/api/v1/me?key=" + _vtok)
check("U9 ?key= 仅凭查询参数 → 能力面未认证",
      r.status_code == 401, f"{r.status_code} {r.text[:80]}")
_cur.execute("update users set status='active' where id=%s", (_uid,))

# ═══ U10: historical runtime paths absent ═══
# C9: bcrypt-only password verification — no legacy format parser
from security import hash_password as _hash_pw
_BCRYPT_OK = _hash_pw("pw12345678")
_LEGACY_HASH = "c2FsdA==:aGFzaA=="  # salt_b64:hash_b64 shape — retired format
_sec_src = open(_os.path.join(_REPO, "core", "security.py"), encoding="utf-8").read()
check("U10 security.py 无 legacy password parser (salt_b64)",
      "salt_b64" not in _sec_src)
check("U10 needs_rehash absent", "needs_rehash" not in _sec_src)
_auth_src = open(_os.path.join(_REPO, "core", "identity", "auth.py"),
                 encoding="utf-8").read()
check("U10 login 无 migration-on-login rehash",
      "needs_rehash" not in _auth_src)
import security as _security_mod
check("U10 verify_password: valid bcrypt → True",
      _security_mod.verify_password("pw12345678", _BCRYPT_OK))
check("U10 verify_password: malformed → False",
      not _security_mod.verify_password("pw12345678", "not-a-hash"))
check("U10 verify_password: legacy salt:hash format → False (fail closed)",
      not _security_mod.verify_password("pw12345678", _LEGACY_HASH))
check("U10 verify_password: empty stored → False",
      not _security_mod.verify_password("pw12345678", ""))
check("U10 verify_password: wrong password on bcrypt → False",
      not _security_mod.verify_password("wrong-pass", _BCRYPT_OK))
# C10: historical tool_call insert writers absent
_ledger_src = open(_os.path.join(_REPO, "core", "repo", "ledger.py"),
                   encoding="utf-8").read()
check("U10 ledger.py 无 insert_call_ok/insert_call_failed",
      "insert_call_ok" not in _ledger_src and "insert_call_failed" not in _ledger_src)
_exec_src = open(_os.path.join(_REPO, "core", "execution", "__init__.py"),
                 encoding="utf-8").read()
check("U10 execution/__init__ 无 historical writer re-exports",
      "insert_call_ok" not in _exec_src and "insert_call_failed" not in _exec_src)
import repo.ledger as _ledger_mod
check("U10 ledger 模块无 historical writer 符号",
      not hasattr(_ledger_mod, "insert_call_ok")
      and not hasattr(_ledger_mod, "insert_call_failed"))
# C25: deploy runbook deletes (never archives/restores) the dead app sqlite
_doc2 = open(_os.path.join(_REPO, "docs", "plan",
                           "ARCHITECTURE_UNIFICATION_DEPLOY_20260916.md"),
             encoding="utf-8").read()
check("U10 deploy runbook 无 sqlite archive/restore 路径",
      "retired-" not in _doc2 and "mv /var/lib/flashshare" not in _doc2
      and "/var/backups/flashshare/retired" not in _doc2)
check("U10 deploy runbook 明确 rm dead sqlite file",
      "rm -f /var/lib/flashshare/flashshare.sqlite" in _doc2)

# ═══ U7: v1 /me 无 plan 键 ═══
_api = open(_os.path.join(_REPO, "app", "routes", "apiv1.py"), encoding="utf-8").read()
check("U7 v1 /me 无 plan alias", '"plan":' not in _api)

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

# ═══ U8: deploy artifact contracts ═══
# (a) cleanup SQL vocabulary snapshot == sorted(VOCAB_EXTS)
import re as _re
_sql_path = _os.path.join(_REPO, "docs", "plan",
                          "ARCHITECTURE_UNIFICATION_DB_CLEANUP.sql")
if not _os.path.exists(_sql_path):
    check("U8 cleanup SQL exists", False, "absent")
else:
    _sqltxt = open(_sql_path, encoding="utf-8").read()
    _i = _sqltxt.rindex("allowed_exts(ext)")
    _vals = _re.findall(r"\('(\.[a-z0-9]+)'\)", _sqltxt[:_i][-4000:])
    check("U8 cleanup SQL vocabulary == sorted(VOCAB_EXTS)",
          sorted(_vals) == sorted(VOCAB_EXTS) and len(_vals) == len(VOCAB_EXTS),
          f"sql={len(_vals)} code={len(VOCAB_EXTS)}")
    _post = _sqltxt.split("Postconditions")[1]
    check("U8 cleanup SQL 无 regex/LIKE 近似判定 (postcondition)",
          "NOT LIKE" not in _post and "!~" not in _post)
    check("U8 cleanup SQL 单事务 (BEGIN/COMMIT)",
          _sqltxt.count("BEGIN;") == 1 and _sqltxt.count("COMMIT;") == 1)
    check("U8 cleanup SQL 无 shell 命令 (#!)",
          "#!/" not in _sqltxt)
# (b) backup rotation tolerates empty dirs under set -euo pipefail
import subprocess as _sp
_rot_sh = (
    "set -euo pipefail\n"
    "KEEP=4\n"
    "for kind in daily weekly; do\n"
    "  D=$(mktemp -d)\n"
    '  { ls -1t "$D"/pg-*.dump 2>/dev/null || true; } |\n'
    "  tail -n +$((KEEP + 1)) | while read -r f; do rm -f \"$f\"; done\n"
    '  echo "$kind-empty-ok"\n'
    '  rm -rf "$D"\n'
    "done\n")
_rot = _sp.run(["bash", "-c", _rot_sh], capture_output=True, text=True)
check("U8 backup rotation: empty daily+weekly under pipefail → exit 0",
      _rot.returncode == 0 and _rot.stdout.count("-empty-ok") == 2,
      _rot.stderr[:120])
# (c) deploy doc: target-worktree backup + quiesced window + rollback boundary
_doc = open(_os.path.join(_REPO, "docs", "plan",
                          "ARCHITECTURE_UNIFICATION_DEPLOY_20260916.md"),
            encoding="utf-8").read()
_backup_lines = [ln.strip() for ln in _doc.splitlines()
                 if "scripts/backup.sh" in ln]
check("U8 deploy doc: backup 命令唯一且来自 /tmp target worktree",
      len(_backup_lines) == 1
      and "/tmp/flashshare-deploy-$TARGET_SHA/scripts/backup.sh"
          in _backup_lines[0],
      _backup_lines)
check("U8 deploy doc: migration 走 target worktree SQL",
      "flashshare-deploy-$TARGET_SHA/docs/plan/ARCHITECTURE_UNIFICATION_DB_CLEANUP.sql" in _doc)
check("U8 deploy doc: quiesced 顺序 (stop → SQL → ff → start)",
      _doc.index("systemctl stop flashshare-api")
      < _doc.index("ARCHITECTURE_UNIFICATION_DB_CLEANUP.sql")
      < _doc.index("git merge --ff-only")
      < _doc.index("systemctl start flashshare-api"))
check("U8 deploy doc: rollback boundary 双分界 (transaction fail vs post-COMMIT)",
      "Migration transaction fails" in _doc
      and "AFTER migration COMMIT" in _doc)
check("U8 deploy doc: authority preflight (SHA/clean/active/DB identity 先于写)",
      "Authority preflight" in _doc and "TARGET_SHA" in _doc.split("## 1.")[0])
check("U8 deploy doc: 无无效 ?| 空 array verify",
      "?| ARRAY[]" not in _doc)


# ═══ U11: greenfield runtime absence guards (module introspection) ═══
# The greenfield codebase keeps ONE canonical payment path; the retired
# compatibility surface must stay absent (introspection, not source copy).
import inspect as _inspect
import importlib as _importlib

_economy_creem = _importlib.import_module("economy.creem")
_repo_orders_mod = _importlib.import_module("repo.orders")
_economy_sub = _importlib.import_module("economy.subscription")
_gov_commission = _importlib.import_module("governance.commission")
_utils_mod = _importlib.import_module("utils")

check("U11 economy.creem 无 legacy fulfill (唯一入口 fulfill_by_request_id)",
      not hasattr(_economy_creem, "fulfill")
      and hasattr(_economy_creem, "fulfill_by_request_id"))
check("U11 repo.orders 无 mark_paid",
      not hasattr(_repo_orders_mod, "mark_paid"))
check("U11 repo.orders 无 mark_subscription_checkout_completed",
      not hasattr(_repo_orders_mod, "mark_subscription_checkout_completed"))
check("U11 repo.orders 无 order_by_provider_id_for_update",
      not hasattr(_repo_orders_mod, "order_by_provider_id_for_update"))
check("U11 repo.orders 保留 canonical helpers (order_by_provider_id /"
      " plan_order_by_sub_id / membership_by_sub)",
      hasattr(_repo_orders_mod, "order_by_provider_id")
      and hasattr(_repo_orders_mod, "plan_order_by_sub_id")
      and hasattr(_repo_orders_mod, "membership_by_sub"))

def _param_required(fn, name):
    p = _inspect.signature(fn).parameters[name]
    return p.default is _inspect.Parameter.empty

check("U11 bind_checkout.order_id 为必填参数 (无 None fallback)",
      _param_required(_economy_sub.bind_checkout, "order_id"))
check("U11 commission_rate.con 为必填参数 (无 standalone read)",
      _param_required(_gov_commission.commission_rate, "con"))

_icon_wrappers = ("upload_icon_svg", "close_icon_svg", "brand_icon_svg",
                  "file_size_icon_svg", "upload_count_icon_svg",
                  "bandwidth_icon_svg", "expiry_icon_svg",
                  "download_count_icon_svg", "history_icon_svg")
check("U11 utils 无 9 个 backward-compat icon wrapper",
      not [w for w in _icon_wrappers if hasattr(_utils_mod, w)])

# ═══ U11: output declaration vocabulary residue guards ═══
# The output declaration mechanism is retired: output identity is
# endpoint-artifact-owned. Production UI/templates/JS must not resurrect
# it. (Server explicit-rejection guards and canonical tests legitimately
# reference the raw field names — those are protocol, not UI.)
import glob as _glob
_ui_files = (
    _glob.glob(_os.path.join(_REPO, "app", "templates", "**", "*.html"),
               recursive=True)
    + _glob.glob(_os.path.join(_REPO, "static", "**", "*.js"),
                 recursive=True))
_residue = []
for _fp in _ui_files:
    if _fp.replace("\\", "/").endswith("static/i18n.js"):
        continue  # generated artifact, guarded below via source dict
    _txt = open(_fp, encoding="utf-8").read()
    for _needle in ('mytools_f_out', 'mytools_err_output_filename_required'):
        if _needle in _txt:
            _residue.append((_fp, _needle))
    # DOM field targeting of the retired payload fields in publish UI
    if _fp.endswith("publish.js"):
        for _needle in ('"output_filename"', '"output_content_type"'):
            if _needle in _txt:
                _residue.append((_fp, _needle))
check("U11 UI/模板/JS 无 output declaration 死键 (mytools_f_out/"
      "mytools_err_output_filename_required)",
      not _residue, str(_residue[:3]))

_i18n_src = open(_os.path.join(_REPO, "core", "i18n.py"),
                 encoding="utf-8").read()
check("U11 canonical i18n 源无死键 (10 语言全删)",
      "mytools_f_out" not in _i18n_src
      and "mytools_err_output_filename_required" not in _i18n_src)
check("U11 guard 文案 = retired-field 语义 (非 filename-required/ext/MIME 语义)",
      "Output declaration field retired" in _i18n_src
      and "Output filename is required" not in _i18n_src
      and "Output extension not allowed" not in _i18n_src
      and "Invalid output type" not in _i18n_src)

_cur.execute("delete from pricing where tool_id like %s or tool_id like 'badaccept%%'", (f"{_u}/%",))
_cur.execute("delete from platform_tools where tool_key like %s or tool_key like 'badaccept%%'", (f"{_u}/%",))
_cur.execute("delete from platform_tools where tool_key like %s", (f"{_u}/%",))
_cur.execute("delete from api_tokens where user_id=%s", (_uid,))
_cur.execute("delete from sessions where user_id=%s", (_uid,))
_cur.execute("delete from users where id=%s", (_uid,))
_con.close()

print()
if FAIL:
    print(f"FAIL: {len(FAIL)} — " + "; ".join(FAIL)); _sys.exit(1)
print(f"FAIL: 无 — 全绿 ({COUNT} checks)")
