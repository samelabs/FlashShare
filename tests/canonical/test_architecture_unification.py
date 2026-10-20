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
from tests.support.cleanup import purge_users  # noqa: E402

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
ok, miss = _cols("platform_tools", ["desc_json", "sort_order", "featured"])
check("U2c platform_tools declaration 列齐", ok, miss)
check("U2c fresh 库无 icon 列", not _cols("platform_tools", ["icon"])[0])
check("U2c fresh 库无 presets_json 列", not _cols("platform_tools", ["presets_json"])[0])
# U2d 格式/tag 参考表 (表驱动): 九 tag (all 恒末位) / 每格式一行归属唯一
# tag / all 无直属行 / 运行态内存结构与表一致
_cur.execute("select key from tags order by sort_order")
_tag_keys = [r[0] for r in _cur.fetchall()]
check("U2d tags = 八场景 + all (all 末位)",
      _tag_keys == ["image", "document", "data", "text", "audio",
                    "video", "cad3d", "archive", "all"], _tag_keys)
_cur.execute("select count(*), count(distinct ext),"
             " count(*) filter (where tag = 'all'),"
             " count(*) filter (where tag not in (select key from tags))"
             " from formats")
_nf, _nd, _n_all, _n_orphan = _cur.fetchone()
from governance.formats import FAMILIES, FAMILY_ORDER, VOCAB_EXTS  # noqa: E402
check("U2d formats 每扩展名一行 = VOCAB 全边界, all 无直属行, 无孤儿",
      _nf == _nd == len(VOCAB_EXTS) == 119 and _n_all == 0 and _n_orphan == 0,
      (_nf, _nd, len(VOCAB_EXTS), _n_all, _n_orphan))
check("U2d 运行态 FAMILIES 由表装载 (八场景, all 不入)",
      list(FAMILIES) == FAMILY_ORDER == _tag_keys[:-1])
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
    base = {"label": "AU", "url": "https://pub.example/x",
            "format_families": "image"}
    base.update(body)
    return client.post("/api/usertools", json=base, cookies={COOKIE_NAME: sess})

r = _post({})  # 无 accept
check("U3 missing accept → 422 bad_accept",
      r.status_code == 422 and r.json().get("error") == "bad_accept", f"{r.status_code} {r.text[:80]}")
r = _post({"accept": None})
check("U3 accept=None → 422 bad_accept",
      r.status_code == 422 and r.json().get("error") == "bad_accept", f"{r.status_code} {r.text[:80]}")
r = _post({"accept": []})
check("U3 accept=[] → 422 bad_accept",
      r.status_code == 422 and r.json().get("error") == "bad_accept", f"{r.status_code} {r.text[:80]}")
r = _post({"accept": [".png"]})
check("U3 explicit valid accept → 200",
      r.status_code == 200 and r.json().get("ok"), f"{r.status_code} {r.text[:160]}")
r = _post({"accept": ["application/pdf"]})  # MIME 不再解释
check("U3 MIME token (非 canonical ext) → 422 bad_accept",
      r.status_code == 422 and r.json().get("error") == "bad_accept", f"{r.status_code} {r.text[:80]}")
r = _post({"accept": [".exe"]})  # 出族
check("U3 out-of-family ext → 422 bad_accept",
      r.status_code == 422 and r.json().get("error") == "bad_accept", f"{r.status_code} {r.text[:80]}")

# ═══ U3b: stored accept semantics (extensions_for_accept 单点) ═══
from governance.formats import extensions_for_accept as _efa
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
    "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
    " label_json, accept_json, format_family, visibility, enabled, trusted,"
    " created_at, updated_at) values (%s, NULL, %s, 'local', '{}',"
    " '[\"application/pdf\"]', 'image', 'public', true, false, %s, %s)",
    ("badaccept", "badaccept", ts, ts))
for _bk, _bj in (("badaccept2", "''"), ("badaccept3", "'null'")):
    _cur.execute("delete from pricing where tool_id=%s", (_bk,))
    _cur.execute("delete from platform_tools where tool_key=%s", (_bk,))
    _cur.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " label_json, accept_json, format_family, visibility, enabled, trusted,"
        " created_at, updated_at) values (%s, NULL, %s, 'local', '{}',"
        " %s, 'image', 'public', true, false, %s, %s)",
        (_bk, _bk, _bj, ts, ts))
_cur.execute(
    "insert into pricing (tool_id, cost, enabled, updated_at) values (%s, 5, true, %s)",
    ("badaccept", ts))
_cur.execute("select coalesce(sum(delta),0) from ledger where user_id=%s", (_uid,))
_l0 = _cur.fetchone()[0]
_cur.execute("select count(*) from tool_calls where user_id=%s", (_uid,))
_tc0 = _cur.fetchone()[0]
from identity.apikeys import create_key as _mk_key
_vtok = _mk_key(_uid, "au-test")["key"]  # Bearer path (MCP)
def _mcp_call(mid, tool, args):
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": mid,
                                     "method": "tools/call",
                                     "params": {"name": tool, "arguments": args}},
                       headers={"Authorization": f"Bearer {_vtok}"})
def _mcp_err(r):
    try:
        return (r.json().get("result", {}).get("content", [{}])[0].get("text", "") or "")
    except Exception:
        return r.text
from economy.billing import grant as _grant28
_grant28(_uid, 500, "manual_topup", idem_key=f"au-u3c:{_uid}", note="au")
_cur.execute("select coalesce(sum(delta),0) from ledger where user_id=%s", (_uid,))
_l0 = _cur.fetchone()[0]
r = _mcp_call(1, "badaccept",
              {"files": [{"filename": "x.png", "b64": "iVBORw0KGgo="}],
               "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 1},
               "client_ref": "au-ref-1"})
_ok_status = r.status_code == 200
_err_txt = _mcp_err(r)
check("U3c invalid stored accept → tool_unavailable (pre-metering)",
      _ok_status and "tool_unavailable" in _err_txt,
      f"{r.status_code} {_err_txt[:80]}")
for _n, _bk in enumerate(("badaccept2", "badaccept3"), start=2):
    r2 = _mcp_call(_n, _bk,
                   {"files": [{"filename": "x.png", "b64": "iVBORw0KGgo="}],
                    "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 1},
                    "client_ref": f"au-ref-{_bk}"})
    check(f"U3c stored {_bk} (''/'null') → pre-metering fail",
          r2.status_code == 200 and "tool_unavailable" in _mcp_err(r2),
          f"{r2.status_code} {_mcp_err(r2)[:80]}")
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
r = client.get("/samelabs", cookies={COOKIE_NAME: sess},
               headers={"Accept": "text/html"}, follow_redirects=False)
check("U9 operator session → /samelabs allowed",
      r.status_code == 200, f"{r.status_code} {r.text[:80]}")
# denial shape follows the caller — non-browser clients (no
# text/html in Accept) get a 401 JSON body, browsers keep the redirect.
r = client.get("/samelabs", headers={"Authorization": f"Bearer {_vtok}"},
               follow_redirects=False)
check("U9 operator bearer token → /samelabs denied (401 JSON)",
      r.status_code == 401 and r.json().get("error") == "operator_auth_required",
      f"{r.status_code} {r.text[:80]}")
r = client.post("/samelabs/tools/x/pricing",
                headers={"Authorization": f"Bearer {_vtok}"},
                follow_redirects=False)
check("U9 operator bearer token → /samelabs POST denied (401 JSON)",
      r.status_code == 401 and r.json().get("error") == "operator_auth_required",
      f"{r.status_code} {r.text[:80]}")
r = client.get("/samelabs?key=" + _vtok, follow_redirects=False)
check("U9 operator token via ?key= → /samelabs denied (401, query auth absent)",
      r.status_code == 401 and r.json().get("error") == "operator_auth_required",
      f"{r.status_code} {r.text[:80]}")
_cur.execute("update users set status='active' where id=%s", (_uid,))
r = client.get("/samelabs", cookies={COOKIE_NAME: sess},
               headers={"Accept": "text/html"}, follow_redirects=False)
check("U9 normal browser session → /samelabs denied (redirect)",
      r.status_code == 302 and "/auth/login" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location', '')}")
r = client.get("/samelabs", headers={"Accept": "text/html"},
               follow_redirects=False)
check("U9 anonymous browser → /samelabs denied (redirect)",
      r.status_code == 302 and "/auth/login" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location', '')}")
# query-token channel is structurally absent from the auth resolver
_main_src = open(_os.path.join(_REPO, "app", "main.py"), encoding="utf-8").read()
check("U9 main.py 无 query key 认证分支",
      'query_params.get("key"' not in _main_src)
check("U9 agents.html 无 ?key= 直连示例",
      "?key=" not in open(_os.path.join(_REPO, "app", "templates", "agents.html"),
                          encoding="utf-8").read())
# Bearer works on the machine surface (MCP), never on browser routes.
_ver9 = open(_os.path.join(_REPO, "VERSION"), encoding="utf-8").read().strip()
r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 91,
                              "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18"}},
                headers={"Authorization": f"Bearer {_vtok}"})
check("U9 Bearer 在 machine surface (/mcp initialize) 有效",
      r.status_code == 200 and "serverInfo" in r.text, f"{r.status_code} {r.text[:80]}")
check("U9 initialize serverInfo.version == root VERSION",
      r.status_code == 200
      and r.json()["result"]["serverInfo"]["version"] == _ver9,
      r.text[:120])
r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 92,
                              "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18"}})
check("U9 anonymous /mcp initialize → 200 discovery (no Bearer needed)",
      r.status_code == 200 and "serverInfo" in r.text, f"{r.status_code}")
r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 93,
                              "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18"}},
                headers={"Authorization": "Bearer fsk_invalid"})
check("U9 invalid Bearer on /mcp → 401", r.status_code == 401, f"{r.status_code}")
r = client.get("/api/me", headers={"Authorization": f"Bearer {_vtok}"})
check("U9 Bearer 在 browser surface (/api/me) 被拒",
      r.status_code == 401, f"{r.status_code} {r.text[:80]}")
r = client.get("/api/v1/me", headers={"Authorization": f"Bearer {_vtok}"})
check("U9 retired /api/v1/me → 404",
      r.status_code == 404, f"{r.status_code} {r.text[:80]}")
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
# C26: governance.platform is orchestration only — no direct SQL/persistence
_pf_src = open(_os.path.join(_REPO, "core", "governance", "platform.py"),
               encoding="utf-8").read()
check("U26 platform.py 无 con.execute 直接 SQL",
      "con.execute" not in _pf_src and "cursor" not in _pf_src)
check("U26 platform.py 无 raw SQL 语句",
      "SELECT " not in _pf_src and "INSERT INTO" not in _pf_src
      and "UPDATE " not in _pf_src and "DELETE FROM" not in _pf_src)
check("U26 platform.py 记账只经 ledger_repo.insert_row",
      "ledger_repo.insert_row" in _pf_src
      and "INSERT INTO ledger" not in _pf_src)
check("U26 platform.py principal/status 只经 users_repo",
      "users_repo.id_by_username" in _pf_src
      and "users_repo.update_status" in _pf_src
      and "UPDATE users" not in _pf_src)
import repo.users as _users_repo_mod
check("U26 users repo 提供 update_status 纯 primitive",
      hasattr(_users_repo_mod, "update_status"))

# C27: ingress receipt persistence/domain authority seam closed
_ig_src = open(_os.path.join(_REPO, "core", "sharing", "ingress_receipt.py"),
               encoding="utf-8").read()
check("U27 ingress_receipt.py 无 con.execute 直接 SQL",
      "con.execute" not in _ig_src and "cursor" not in _ig_src)
check("U27 ingress_receipt.py 无 ingress_requests raw SQL 语句",
      "FROM ingress_requests" not in _ig_src
      and "INSERT INTO ingress_requests" not in _ig_src
      and "UPDATE ingress_requests" not in _ig_src)
check("U27 ingress_receipt claim/finalize/resolve 只经 repo primitives",
      "_repo.earliest_by_ref" in _ig_src and "_repo.live_by_ref" in _ig_src
      and "_repo.insert_running" in _ig_src and "_repo.by_id_for_update" in _ig_src
      and "_repo.running_receipts" in _ig_src
      and "_repo.finalize_ok" in _ig_src and "_repo.settle_failed" in _ig_src)
_ig_repo_src = open(_os.path.join(_REPO, "core", "repo",
                                  "ingress_requests.py"),
                    encoding="utf-8").read()
check("U27 ingress_requests 表 SQL 单一 repo authority",
      "FROM ingress_requests" in _ig_repo_src
      and "INSERT INTO ingress_requests" in _ig_repo_src)
_api_src = open(_os.path.join(_REPO, "app", "routes", "api.py"),
                encoding="utf-8").read()
check("U27 routes/api.py 不直接 import repo.ingress_requests",
      "repo.ingress_requests" not in _api_src
      and "repo import ingress_requests" not in _api_src)
check("U27 routes/api.py receipt finalize 走 canonical sharing finalize",
      "ingress_finalize_ok(con," not in _api_src
      and "_receipt_ok(con," not in _api_src
      and "stage_sources(" in _api_src)
check("U27 sharing finalize_ok 是 caller-transaction 形式 (con, ingress_id)",
      "def finalize_ok(con, ingress_id" in _ig_src)

# C28: Public REST v1 retired — MCP is the only northbound machine surface
import os as _os2
check("U28 app/routes/apiv1.py 不存在",
      not _os2.path.exists(_os2.path.join(_REPO, "app", "routes", "apiv1.py")))
_main28 = open(_os2.path.join(_REPO, "app", "main.py"), encoding="utf-8").read()
check("U28 main.py 不 import/注册 apiv1",
      "apiv1" not in _main28)
check("U28 hosted /tool-endpoints surface deleted (C1)",
      "hosted_tools" not in _main28
      and '"/tool-endpoints"' not in _main28)
check("U28 apiv1 router prefix 无 /api/v1 注册",
      '"/api/v1"' not in _main28)
_mcp28 = open(_os2.path.join(_REPO, "app", "routes", "mcp.py"),
              encoding="utf-8").read()
check("U28 MCP source 无 /api/v1 叙事",
      "/api/v1" not in _mcp28)
_web_auth28 = open(_os2.path.join(_REPO, "app", "web_auth.py"),
                   encoding="utf-8").read()
check("U28 require_session_or_bearer_auth 已删 (browser 面 session-only)",
      "require_session_or_bearer_auth" not in _web_auth28)
check("U28 Bearer 解析仅限 /mcp",
      'path == "/mcp"' in _main28
      and 'path.startswith("/tool-endpoints/")' not in _main28)
# production templates/JS 无 Public REST active expression
_tpl28 = (open(_os2.path.join(_REPO, "app", "templates", "agents.html"),
               encoding="utf-8").read()
          + open(_os2.path.join(_REPO, "app", "templates", "tool_mcp.html"),
                 encoding="utf-8").read())
check("U28 templates 无 /api/v1 能力表达", "/api/v1" not in _tpl28)
_i18n28 = open(_os2.path.join(_REPO, "core", "i18n.py"), encoding="utf-8").read()
check("U28 i18n 无 Public REST 文案键 (agents_ep_*/agents_api_*/tool_mcp_http_hint)",
      all(k not in _i18n28 for k in
          ("agents_ep_me", "agents_api_h", "agents_api_desc2",
           "tool_mcp_http_hint")))
check("U28 hosted execution path fully removed (files + capability)",
      not _os2.path.exists(_os2.path.join(_REPO, "app", "routes",
                                          "hosted_tools.py"))
      and not _os2.path.exists(_os2.path.join(_REPO, "core",
                                              "tools_corners.py"))
      and not _os2.path.exists(_os2.path.join(
          _REPO, "tests", "canonical", "test_rounded_corners.py"))
      and not _os2.path.exists(_os2.path.join(
          _REPO, "tests", "canonical",
          "test_hosted_execution_capability.py"))
      and "sign_hosted_exec_token" not in open(
          _os2.path.join(_REPO, "core", "security.py"),
          encoding="utf-8").read()
      and "X-Hosted-Execution" not in open(
          _os2.path.join(_REPO, "core", "execution", "orchestrator.py"),
          encoding="utf-8").read())

# C29: orders table SQL lives only in repo/orders.py
import glob as _glob29
_orders_sql_files = []
for _f29 in _glob29.glob(_os2.path.join(_REPO, "app", "**", "*.py"), recursive=True) + \
             _glob29.glob(_os2.path.join(_REPO, "core", "**", "*.py"), recursive=True):
    if "__pycache__" in _f29 or _f29.endswith(_os2.path.join("repo", "orders.py")) \
            or _f29.endswith(_os2.path.join("repo", "operator_views.py")):
        # operator_views = S4 designated READ projection repo (select/join/
        # pagination only); orders WRITE authority stays repo/orders.py
        continue
    _src29 = open(_f29, encoding="utf-8", errors="ignore").read()
    if any(_pat29 in _src29 for _pat29 in
           ("FROM orders", "INTO orders", "UPDATE orders")):
        _orders_sql_files.append(_os2.path.relpath(_f29, _REPO))
check("U29 production 非 repo/orders.py 无 orders SQL",
      not _orders_sql_files, str(_orders_sql_files))
_creem29 = open(_os2.path.join(_REPO, "core", "economy", "creem.py"),
                encoding="utf-8").read()
_sub29 = open(_os2.path.join(_REPO, "core", "economy", "subscription.py"),
              encoding="utf-8").read()
check("U29 creem/subscription orders 读走 repo primitive",
      "order_by_id_for_update(con, order_id)" in _creem29
      and "order_by_id_for_update(con, order_id)" in _sub29
      and "FROM orders" not in _creem29 and "FROM orders" not in _sub29)

# C30: HTTP binding vocabulary closure (legacy output fields retired)
_ts30 = open(_os2.path.join(_REPO, "core", "governance", "tool_supply.py"),
             encoding="utf-8").read()
check("C30 tool_supply 无退休 output binding 字段名",
      "output_filename" not in _ts30 and "output_content_type" not in _ts30)
check("C30 binding vocabulary 为封闭四键",
      '_BINDING_KEYS = ("url", "timeout_s", "auth_header", "auth_token")' in _ts30)
sys.path.insert(0, _os2.path.join(_REPO, "core"))
from governance.tool_supply import build_http_binding, DeclarationError  # noqa: E402
_b30 = build_http_binding(
    {"url": "https://vocab.example/api", "timeout_s": 30},
    {"url": "https://old.example", "timeout_s": 90, "auth_header": "X-Key",
     "auth_token": "sealed:x"})
check("C30 build_http_binding 只含四 canonical keys",
      set(_b30) == {"url", "timeout_s", "auth_header", "auth_token"}, _b30)
check("C30 PATCH 保留未修改的合法 canonical fields",
      _b30["auth_header"] == "X-Key" and _b30["timeout_s"] == 30, _b30)
_closed_f = _closed_e = False
try:
    build_http_binding({"url": "https://x.example", "custom_key": "v"})
except DeclarationError:
    _closed_f = True
try:
    build_http_binding({"timeout_s": 60},
                       {"url": "https://o.example", "output_filename": "l.png"})
except DeclarationError:
    _closed_e = True
check("C30 arbitrary unknown binding key → DeclarationError (fail closed)",
      _closed_f and _closed_e)

# C31: tool-input.js accept chips stay canonical-only — no MIME
# compatibility fallback (the input area renderer owns the display)
import re as _re31
_ti31 = open(_os2.path.join(_REPO, "static", "pages", "tool-input.js"),
             encoding="utf-8").read()
_m31 = _re31.search(r"function acceptTokens\(\)\s*\{(.*?)\n    \}", _ti31,
                    _re31.DOTALL)
_accept_body31 = _m31.group(1) if _m31 else ""
check("C31 accept 判定用 canonical token (无 MIME 展示回退)",
      _accept_body31 != "" and "fmt-chip" not in _ti31
      and "chip-row" not in _ti31)
check("C31 accept 判定用 strip 前导点的 canonical token",
      'replace(/^\\./, "")' in _accept_body31)
check("C31 accept 展示无 MIME split(\"/\")",
      'split("/")' not in _ti31)
check("C31 accept 展示无 MIME indexOf(\"/\")",
      'indexOf("/")' not in _ti31)

# C32: single product version authority (root VERSION)
_ver32 = open(_os2.path.join(_REPO, "VERSION"), encoding="utf-8").read()
check("C32 VERSION 为 major.minor 唯一 release authority",
      _ver32.endswith("\n") and _ver32.count("\n") == 1
      and _re31.fullmatch(r"[0-9]+\.[0-9]+", _ver32.strip()))
sys.path.insert(0, _os2.path.join(_REPO, "core"))
from config import PRODUCT_VERSION as _PV32  # noqa: E402
check("C32 runtime PRODUCT_VERSION == root VERSION",
      _PV32 == _ver32.strip())
_mcp32 = open(_os2.path.join(_REPO, "app", "routes", "mcp.py"),
              encoding="utf-8").read()
check("C32 MCP serverInfo.version 消费 derived PRODUCT_VERSION",
      '"version": PRODUCT_VERSION' in _mcp32)
check("C32 mcp.py 无硬编码 1.0.0 产品版本", '"1.0.0"' not in _mcp32)

# C33: user tool PATCH/DELETE — platform_tools + pricing in ONE transaction
_ut33 = open(_os2.path.join(_REPO, "app", "routes", "usertools.py"),
             encoding="utf-8").read()
import re as _re33
_patch_fn33 = _re33.search(
    r"async def update_user_tool.*?(?=\n@router|\nasync def )", _ut33, _re33.DOTALL)
_del_fn33 = _re33.search(
    r"async def delete_user_tool.*", _ut33, _re33.DOTALL)
check("C33 PATCH 单事务: update_tool(con=con) 与 set_pricing(con=con) 同一 db()",
      _patch_fn33 is not None
      and _patch_fn33.group(0).count("with db() as con:") == 1
      and "update_tool(tool_key, sets, con=con)" in _patch_fn33.group(0)
      and 'admin_user_id=user["id"], con=con' in _patch_fn33.group(0))
check("C33 PATCH snapshot 行锁在事务内 (owner_row_for_update)",
      _patch_fn33 is not None
      and 'owner_row_for_update(con, tool_key, user["id"])' in _patch_fn33.group(0)
      and "owner_row_by_key" not in _patch_fn33.group(0))
check("C33 DELETE 单事务: delete_tool_row 与 set_pricing(con=con) 同一 db()",
      _del_fn33 is not None
      and _del_fn33.group(0).count("with db() as con:") == 1
      and 'delete_tool_row(con, row["id"])' in _del_fn33.group(0)
      and 'admin_user_id=user["id"], con=con' in _del_fn33.group(0)
      and _del_fn33.group(0).find("delete_tool_row") <
          _del_fn33.group(0).find("set_pricing"))

# C34: platform_tools declaration partial writer is single and canonical
_cat34 = open(_os2.path.join(_REPO, "core", "repo", "catalog.py"),
              encoding="utf-8").read()
_ts34 = open(_os2.path.join(_REPO, "core", "governance", "tool_supply.py"),
             encoding="utf-8").read()
check("C34 repo.catalog 无 dead duplicate writer update_fields",
      "def update_fields" not in _cat34)
check("C34 declaration partial writer 仍为 update_tool_row 且 tool_supply 消费",
      "def update_tool_row" in _cat34
      and "from repo.catalog import update_tool_row" in _ts34)

# C35: backoffice tool audit projection reads via repo primitives
# (S4 batch A: projection migrated from the retired route monolith into
#  core/backoffice/tools.py)
_sl35 = open(_os2.path.join(_REPO, "core", "backoffice", "tools.py"),
             encoding="utf-8").read()
_proj35 = _re33.search(
    r"def _safe_remote_projection.*?(?=\ndef )", _sl35, _re33.DOTALL)
check("C35 _safe_remote_projection 无 route-layer con.execute",
      _proj35 is not None and "con.execute" not in _proj35.group(0))
check("C35 _safe_remote_projection 消费 repo.catalog canonical read primitives",
      _proj35 is not None
      and "by_tool_key(con, tool_key)" in _proj35.group(0)
      and "pricing_row(con, tool_key)" in _proj35.group(0))

# C36: products page membership counts read via repo.orders primitives
_sl36 = open(_os2.path.join(_REPO, "app", "routes", "samelabs",
                            "commerce.py"),
             encoding="utf-8").read()
_plans36 = _re33.search(
    r"async def products.*?(?=\n@router|\nasync def )", _sl36,
    _re33.DOTALL)
if _plans36 is None:  # last-function fallback
    _plans36 = _re33.search(r"async def products.*", _sl36, _re33.DOTALL)
check("C36 samelabs_plans 无 route-layer memberships/pending_downgrades SQL",
      _plans36 is not None
      and "FROM memberships" not in _plans36.group(0)
      and "FROM pending_downgrades" not in _plans36.group(0))
check("C36 samelabs_plans 计数消费 repo.orders primitives",
      _plans36 is not None
      and "_orders_repo.membership_counts_by_plan(con, _ts)" in _plans36.group(0)
      and "_orders_repo.pending_downgrade_counts_by_plan(con)" in _plans36.group(0))
check("C36 samelabs_plans 单请求单一 ts (now() 一次, 传入 effective count)",
      _plans36 is not None
      and "_ts = now()" in _plans36.group(0)
      and _plans36.group(0).count("now()") == 1)

# C37: plan mirror commit-outcome classifier reads via repo primitives
_cls37 = _re33.search(
    r"def _classify_db_commit_outcome.*?(?=\ndef )", open(
        _os2.path.join(_REPO, "core", "backoffice", "products.py"),
        encoding="utf-8").read(), _re33.DOTALL)
check("C37 classifier 函数体无 con.execute",
      _cls37 is not None and "con.execute" not in _cls37.group(0))
check("C37 classifier 消费 repo.orders.plan_by_id + repo.operator_audit read primitive",
      _cls37 is not None
      and "_plan_by_id(con, plan_id)" in _cls37.group(0)
      and "_recent_pair_rows(" in _cls37.group(0))
check("C37 classifier 仍用独立 fresh connection",
      _cls37 is not None and "with _fdb() as con:" in _cls37.group(0))

# C38: catalog startup invariant reads via repo primitive
_gcat38 = open(_os2.path.join(_REPO, "core", "governance", "catalog.py"),
               encoding="utf-8").read()
_val38 = _re33.search(
    r"def validate_catalog_rows.*?(?=\ndef )", _gcat38, _re33.DOTALL)
check("C38 validate_catalog_rows 函数体无 con.execute",
      _val38 is not None and "con.execute" not in _val38.group(0))
check("C38 validate_catalog_rows 消费 repo.catalog.catalog_contract_rows",
      _val38 is not None
      and "catalog_contract_rows(con)" in _val38.group(0))
check("C38 startup invariant 校验 execution ∈ canonical supply vocabulary",
      _val38 is not None
      and "from governance.tool_supply import" in _val38.group(0)
      and "_EXECUTIONS" in _val38.group(0)
      and 'r["execution"] not in _EXECUTIONS' in _val38.group(0))
_disp58 = open(_os2.path.join(_REPO, "core", "execution", "dispatch.py"),
               encoding="utf-8").read()
check("C58 dispatch 无 execution='mcp' 当 remote 的分支",
      '"http", "mcp"' not in _disp58)
_pre58 = _disp58.find("tool.get(\"execution\") not in _EXECUTIONS")
_gate58 = _disp58.find("gate_run(")
check("C58 dispatch execution vocabulary 校验在 metering gate 之前",
      0 < _pre58 < _gate58)
check("C38 parse_stored_accept 仍由 governance 层调用",
      _val38 is not None
      and "from governance.formats import parse_stored_accept" in _val38.group(0))

# C39: subscription reversal ledger read via repo primitive
_rev39_src = open(_os2.path.join(_REPO, "core", "economy", "reverse.py"),
                  encoding="utf-8").read()
_rev39 = _re33.search(
    r"def _reverse_subscription.*?(?=\ndef )", _rev39_src, _re33.DOTALL)
check("C39 _reverse_subscription 函数体无 con.execute",
      _rev39 is not None and "con.execute" not in _rev39.group(0))
check("C39 _reverse_subscription 消费 repo.ledger subscription grant primitive",
      _rev39 is not None
      and "subscription_grants_by_transaction(con, user_id," in _rev39.group(0))
check("C39 total 求和仍留 economy 层",
      _rev39 is not None
      and 'total = sum(int(r["delta"]) for r in rows)' in _rev39.group(0))

# C40: subscription replay probe reads via the same repo primitive
_sub40_src = open(_os2.path.join(_REPO, "core", "economy", "subscription.py"),
                  encoding="utf-8").read()
_paid40 = _re33.search(
    r"def apply_paid_period.*?(?=\ndef )", _sub40_src, _re33.DOTALL)
check("C40 apply_paid_period 函数体无 con.execute",
      _paid40 is not None and "con.execute" not in _paid40.group(0))
check("C40 replay 判定消费 subscription_grants_by_transaction (无第二个 exists primitive)",
      _paid40 is not None
      and "subscription_grants_by_transaction(" in _paid40.group(0))
check("C40 _is_replay 判定仍留 economy 层",
      _paid40 is not None
      and "_is_replay = bool(_seen_rows)" in _paid40.group(0))

# C41: execution tool_calls reads via repo primitives
_gate41_src = open(_os2.path.join(_REPO, "core", "execution", "gate.py"),
                   encoding="utf-8").read()
_rec41 = _re33.search(
    r"def recover_abandoned_calls.*?(?=\ndef )", _gate41_src, _re33.DOTALL)
check("C41 recover_abandoned_calls 函数体无 con.execute",
      _rec41 is not None and "con.execute" not in _rec41.group(0))
check("C41 recovery 消费 running-call repo primitive",
      _rec41 is not None and "rows = running_calls(con)" in _rec41.group(0))
_out41 = _re33.search(
    r"def resolve_success_outcome.*?(?=\\nasync def )", _gate41_src, _re33.DOTALL)
if _out41 is None:
    _out41 = _re33.search(r"def resolve_success_outcome.*",
                          _gate41_src, _re33.DOTALL)
check("C41 resolve_success_outcome 函数体无 con.execute",
      _out41 is not None and "con.execute" not in _out41.group(0))
check("C41 commit-outcome 消费 call_outcome_for_update primitive",
      _out41 is not None
      and "call_outcome_for_update(con, call_id)" in _out41.group(0))
_led41 = open(_os2.path.join(_REPO, "core", "repo", "ledger.py"),
              encoding="utf-8").read()
check("C41 FOR UPDATE 保留在 repo primitive 中",
      'SELECT status, result_json FROM tool_calls'
      in _led41 and "FOR UPDATE" in _led41)

# C42: init_db runtime cleanup consumes repo primitives
_db42 = open(_os2.path.join(_REPO, "core", "database.py"),
             encoding="utf-8").read()
_init42 = _re33.search(r"def init_db.*?(?=\ndef )", _db42, _re33.DOTALL)
if _init42 is None:
    _init42 = _re33.search(r"def init_db.*", _db42, _re33.DOTALL)
check("C42 init_db 函数体无三个 direct DELETE",
      _init42 is not None
      and "DELETE FROM oauth_states" not in _init42.group(0)
      and "DELETE FROM sessions" not in _init42.group(0)
      and "DELETE FROM upload_usage" not in _init42.group(0))
check("C42 init_db 消费 repo cleanup primitives",
      _init42 is not None
      and "delete_expired_oauth(con, ts)" in _init42.group(0)
      and "delete_expired(con, ts)" in _init42.group(0)
      and "purge_before(con, ts - 24 * 3600)" in _init42.group(0))
check("C42 Free Tier bootstrap INSERT INTO plans 保留",
      _init42 is not None and "INSERT INTO plans" in _init42.group(0))

# C43: no internal prototype assets in the production static tree
check("C43 production static tree 无内部原型目录 static/proto",
      not _os2.path.exists(_os2.path.join(_REPO, "static", "proto"))
      and not _os2.path.isdir(_os2.path.join(_REPO, "static", "proto")))

# C44: privacy handler consumes canonical params only (no silent fallback)
_tr44 = open(_os2.path.join(_REPO, "core", "tools_registry.py"),
             encoding="utf-8").read()
_priv44 = _re33.search(
    r"def privacy_handler.*?(?=\ndef |\nclass )", _tr44, _re33.DOTALL)
check("C44 privacy_handler 无 silent preset fallback",
      _priv44 is not None
      and 'params.get("preset"' not in _priv44.group(0)
      and "PRESETS.get(" not in _priv44.group(0))
check("C44 privacy_handler 直接消费 canonical preset",
      _priv44 is not None
      and 'preset = params["preset"]' in _priv44.group(0)
      and 'p = PRESETS[preset]' in _priv44.group(0))

# C45: remaining first-party registered enum-param handlers consume
# canonical params only (no silent default/fallback)
def _body45(fname, fnpat):
    t = open(_os2.path.join(_REPO, "core", fname), encoding="utf-8").read()
    m = _re33.search(fnpat, t, _re33.DOTALL)
    return m.group(0) if m else ""
_cp45 = _body45("tools_compress.py", r"def compress_handler.*?(?=\ndef |\nregister\()")
_by45 = _body45("tools_beautify.py", r"def beautify_handler.*?(?=\ndef |\nregister\()")
_ic45 = _body45("tools_ico.py", r"def ico_handler.*?(?=\ndef |\nregister\()")
for _nm45, _b45, _d45 in (
        ("compress", _cp45, "TARGETS.get("),
        ("beautify", _by45, "PRESETS.get("),
        ("ico", _ic45, "ICO_PRESETS.get(")):
    check(f"C45 {_nm45} handler 无 silent preset fallback",
          _b45 != "" and 'params.get("preset"' not in _b45
          and _d45 not in _b45)
    check(f"C45 {_nm45} handler 直接消费 canonical preset",
          _b45 != "" and 'preset = params["preset"]' in _b45)

# C46: generic Gokapi concrete operations are only used by the storage
# adapter (and its gokapi implementation); production callers go through
# storage.write/read/delete/info
_gen46 = ("upload_to_gokapi", "get_gokapi_file", "delete_gokapi_file")
for _root46, _dirs46, _fs46 in _os2.walk(_REPO):
    if ".git" in _root46.split(_os2.sep):
        continue
    for _f46 in _fs46:
        if not _f46.endswith(".py") or "__pycache__" in _root46:
            continue
        _p46 = _os2.path.join(_root46, _f46)
        _rel46 = _os2.path.relpath(_p46, _REPO)
        if _rel46.startswith(("core/gokapi.py", "core/storage.py", "tests/")):
            continue  # adapter+impl own the concrete ops; tests patch them
        _t46 = open(_p46, encoding="utf-8").read()
        for _sym46 in _gen46:
            check(f"C46 {_rel46} 不直接使用 concrete {_sym46}",
                  _sym46 not in _t46)

# C47: Gokapi /api/files/list transport is owned by the gokapi adapter;
# sharing owns only presentation cache policy
_up47 = open(_os2.path.join(_REPO, "core", "sharing", "uploads.py"),
             encoding="utf-8").read()
check("C47 sharing/uploads 不引用 private Gokapi transport internals",
      "_gokapi_connection" not in _up47
      and "_gokapi_path" not in _up47
      and "GOKAPI_KEY" not in _up47)
_gstat47 = _re33.search(r"def _get_all_gokapi_status.*?(?=\ndef )",
                        _up47, _re33.DOTALL)
check("C47 _get_all_gokapi_status 消费 list_gokapi_files",
      _gstat47 is not None and "list_gokapi_files(timeout=2)" in _gstat47.group(0)
      and "/api/files/list" not in _gstat47.group(0))
for _root47, _dirs47, _fs47 in _os2.walk(_REPO):
    if ".git" in _root47.split(_os2.sep):
        continue
    for _f47 in _fs47:
        if not _f47.endswith(".py") or "__pycache__" in _root47:
            continue
        _rel47 = _os2.path.relpath(_os2.path.join(_root47, _f47), _REPO)
        if _rel47 != "core/gokapi.py" and not _rel47.startswith("tests/"):
            _t47 = open(_os2.path.join(_root47, _f47), encoding="utf-8").read()
            check(f"C47 {_rel47} 无 /api/files/list transport",
                  '"/api/files/list"' not in _t47)

# C48: no dead storage_read re-export in execution/source — canonical read
# callers import storage.read directly
_src48 = open(_os2.path.join(_REPO, "core", "execution", "source.py"),
              encoding="utf-8").read()
check("C48 execution/source 无 storage_read re-export",
      "storage_read" not in _src48)

_exec_src = open(_os.path.join(_REPO, "core", "execution", "__init__.py"),
                 encoding="utf-8").read()
check("U10 execution/__init__ 无 historical writer re-exports",
      "insert_call_ok" not in _exec_src and "insert_call_failed" not in _exec_src)
import repo.ledger as _ledger_mod
check("U10 ledger 模块无 historical writer 符号",
      not hasattr(_ledger_mod, "insert_call_ok")
      and not hasattr(_ledger_mod, "insert_call_failed"))
# ═══ U7: Public REST v1 retired ═══
check("U7 app/routes/apiv1.py 不存在",
      not _os.path.exists(_os.path.join(_REPO, "app", "routes", "apiv1.py")))

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
import re as _re11
for _fp in _ui_files:
    if _fp.replace("\\", "/").endswith("static/i18n.js"):
        continue  # generated artifact, guarded below via source dict
    _txt = open(_fp, encoding="utf-8").read()
    for _needle in ('mytools_f_out', 'mytools_err_output_filename_required'):
        # whole-key match: the retired bare key is banned; the IO-2
        # outputs selector key (mytools_f_outputs) is a live, different key
        if _re11.search(_needle + r'(?![_a-z0-9])', _txt):
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
      '"mytools_f_out":' not in _i18n_src
      and '"mytools_err_output_filename_required"' not in _i18n_src)
check("U11 guard 文案 = retired-field 语义 (非 filename-required/ext/MIME 语义)",
      "Output declaration field retired" in _i18n_src
      and "Output filename is required" not in _i18n_src
      and "Output extension not allowed" not in _i18n_src
      and "Invalid output type" not in _i18n_src)

_cur.execute("delete from pricing where tool_id like %s or tool_id like 'badaccept%%'", (f"{_u}_%",))
_cur.execute("delete from platform_tools where tool_key like %s or tool_key like 'badaccept%%'", (f"{_u}_%",))
_cur.execute("delete from platform_tools where tool_key like %s", (f"{_u}_%",))
_cur.execute("delete from api_keys where user_id=%s", (_uid,))
_cur.execute("delete from sessions where user_id=%s", (_uid,))
purge_users(_cur, [_uid])
_con.close()

print()
if FAIL:
    print(f"FAIL: {len(FAIL)} — " + "; ".join(FAIL)); _sys.exit(1)
print(f"FAIL: 无 — 全绿 ({COUNT} checks)")
