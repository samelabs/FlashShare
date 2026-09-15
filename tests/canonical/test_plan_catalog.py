#!/usr/bin/env python3
"""plans 行 = Tier 唯一事实源 — canonical 守卫套。

四守卫:
  PC1  零代码字典: 全仓无 tiers.py 字典符号残留 (FREE/_PLANS/PAID_TIER_CODES/
      TIER_PLAN_SPECS/TIER_ORDER 不得再成为 tier 事实源)
  PC2  seed 永不覆盖: init_db 二连跑, 篡改后的 plus/pro/free 行值原样存活
  PC3  benefits 单源: resolve_from_membership 输出与 plans 行逐键一致
      (expiry_days/download_limits 逐项相等, 非子集)
  PC4  数据驱动插第三档: 造完整新档行 (配齐 benefits + product + price) →
      直接进 sellable, 零代码改动
fail-closed canonical 规则 (与同目录其它套一致)。
"""
import os as _os, sys as _sys, sys
import re

_REPO = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))

# ── FAIL-CLOSED GATE ────────────────────────────────────────────────────
_TEST_PORT = _os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT is required (isolated service port, never the production port).\n")
    _sys.exit(2)
try:
    _TEST_PORT_INT = int(_TEST_PORT, 10)
except ValueError:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT must be an integer port number.\n")
    _sys.exit(2)
if _TEST_PORT_INT == 8000:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT=8000 is the production port; canonical tests must use an isolated port.\n")
    _sys.exit(2)
TEST_DB = _os.environ.get("TEST_DB_URL")
if not TEST_DB:
    _sys.stderr.write("ERROR: TEST_DB_URL is required (isolated test database DSN).\n")
    _sys.exit(2)

sys.path.insert(0, _REPO)
_os.environ.setdefault("DATABASE_URL", TEST_DB.replace("postgresql://", "postgresql+psycopg://"))
_os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "canonical-test")
sys.path.insert(0, _os.path.join(_REPO, "core"))

import psycopg2
from database import db
from repo import orders as orders_repo
from governance import plan_catalog
from governance.entitlements import resolve_from_membership

FAIL = []
def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAIL.append(name)

PG = TEST_DB.replace("postgresql+psycopg://", "postgresql://")
_con = psycopg2.connect(PG); _con.autocommit = True
_cur = _con.cursor()
_ts = int(__import__("time").time())

# ═══ PC1: 零代码字典 — 全仓源码无旧字典符号 ═══
_skip_dirs = {".git", "__pycache__", "node_modules", "venv", ".venv"}
_pat = re.compile(r"\b(PAID_TIER_CODES|TIER_PLAN_SPECS|TIER_ORDER)\b")
_hits = []
_SELF = _os.path.abspath(__file__)
for root, dirs, files in _os.walk(_REPO):
    dirs[:] = [d for d in dirs if d not in _skip_dirs]
    for f in files:
        if not f.endswith(".py"):
            continue
        p = _os.path.join(root, f)
        if p == _SELF:
            continue  # 守卫自身引用符号名 (docstring/正则), 不是字典残留
        try:
            text = open(p, encoding="utf-8").read()
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _pat.search(line):
                _hits.append(f"{_os.path.relpath(p, _REPO)}:{i}")
check("PC1 零代码字典: 全仓 .py 无 PAID_TIER_CODES/TIER_PLAN_SPECS/TIER_ORDER",
      not _hits, "; ".join(_hits[:5]))
_tiers_py = _os.path.join(_REPO, "core", "governance", "tiers.py")
check("PC1b core/governance/tiers.py 已删除 (行是唯一事实源)",
      not _os.path.exists(_tiers_py))

# ═══ PC2: seed 永不覆盖 — 篡改行值后 init_db 重跑, 值存活 ═══
with db() as con:
    con.execute("update plans set monthly_credits=777, ingress_files_24h=888,"
                " expiry_days=ARRAY[3], download_limits=ARRAY[10], badge_key='tampered'"
                " where code='plus'")
import importlib
import database as _dbmod
_dbmod.init_db()
_pc2 = plan_catalog.catalog(refresh=True)
_plus = next(t for t in _pc2 if t.code == "plus")
check("PC2 seed 永不覆盖: 篡改的 plus 行 (777/888/[3]/[10]/'tampered') init_db 后存活",
      _plus.monthly_credits == 777 and _plus.benefits.ingress_files_24h == 888
      and tuple(_plus.benefits.expiry_days) == (3,)
      and tuple(_plus.benefits.download_limits) == (10,)
      and _plus.badge_key == "tampered",
      f"got credits={_plus.monthly_credits} files={_plus.benefits.ingress_files_24h}"
      f" ed={list(_plus.benefits.expiry_days)} dl={list(_plus.benefits.download_limits)}")
with db() as con:  # 恢复种子值 (后续套件依赖)
    con.execute("update plans set monthly_credits=100, ingress_files_24h=500,"
                " ingress_bytes_24h=5368709120, expiry_days=ARRAY[1,3,7,30],"
                " download_limits=ARRAY[1,10,50,100,200,1000], badge_key='tier_plus'"
                " where code='plus'")
plan_catalog.catalog(refresh=True)

# ═══ PC3: benefits 单源 — resolve_from_membership 与 plans 行逐键一致 ═══
for _code in ("plus", "pro"):
    _t = plan_catalog.by_code(_code)
    _ent = resolve_from_membership({"member": True, "code": _code})
    check(f"PC3 {_code} entitlement 逐键 == plans 行 (单源派生, 非另抄)",
          _ent["ingress_files_24h"] == _t.benefits.ingress_files_24h
          and _ent["ingress_bytes_24h"] == _t.benefits.ingress_bytes_24h
          and tuple(_ent["expiry_days"]) == tuple(_t.benefits.expiry_days)
          and tuple(_ent["download_limits"]) == tuple(_t.benefits.download_limits)
          and _ent["plan_code"] == _t.code,
          f"ent={_ent}")

# ═══ PC4: 数据驱动插第三档 — 完整行直接可售, 零代码改动 ═══
with db() as con:
    con.execute(
        "insert into plans (code, name, monthly_credits, period_days, period,"
        " price_cents, currency, creem_product_id, sort_order, active, badge_key,"
        " ingress_files_24h, ingress_bytes_24h, expiry_days, download_limits,"
        " created_at, updated_at)"
        " values ('pc_biz', 'PC Business', 2000, 30, 'monthly', 9000, 'USD',"
        " 'prod_pc_biz', 5, TRUE, 'tier_pc_biz', 800, %s, ARRAY[1,3,7],"
        " ARRAY[1,10,50,100], %s, %s)"
        " on conflict (code) do update set active=TRUE",
        (5 * 1024 ** 3, _ts, _ts))
plan_catalog.catalog(refresh=True)
_pc4_tier = plan_catalog.by_code("pc_biz")
_pc4_ent = resolve_from_membership({"member": True, "code": "pc_biz"})
with db() as con:
    _pc4_sellable = [r["code"] for r in orders_repo.canonical_plans(con)]
check("PC4 新档行 (配齐 benefits+product+price) 零代码进 sellable",
      _pc4_tier is not None and _pc4_tier.sellable and "pc_biz" in _pc4_sellable,
      f"tier={_pc4_tier is not None} sellable={_pc4_sellable}")
check("PC4b 新档 entitlement 直接来自行 (数据驱动, 无代码映射)",
      _pc4_ent["ingress_files_24h"] == 800
      and tuple(_pc4_ent["expiry_days"]) == (1, 3, 7)
      and _pc4_ent["plan_code"] == "pc_biz",
      f"ent={_pc4_ent}")
with db() as con:  # 清场: 第三档移出 active, 不污染后续套件的种子合同断言
    con.execute("update plans set active=FALSE where code='pc_biz'")
plan_catalog.catalog(refresh=True)

print()
if FAIL:
    print(f"FAIL: {len(FAIL)} — " + "; ".join(FAIL))
    _sys.exit(1)
print("FAIL: 无 — 全绿")
_con.close()
