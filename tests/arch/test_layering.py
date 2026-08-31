"""分层架构断言 (批0 裁判) — unittest, 零依赖, 直接 python3 运行。

五断言 (A1-A5), 白名单机制: 重构期现状违例清单化, 批1/批2/批3 逐域清零,
批4 结束白名单必须为空。运行:
    /opt/flashshare/venv/bin/python3 tests/arch/test_layering.py

检查器语义:
  A1 SQL 文本(多行感知)仅允许出现在 core/repo/** 与 core/database.py
  A2 app/** 不得 with db()/con.execute
  A3 同函数内 con.* 用途: 或 con 是该函数参数(事务传递), 或落在
     `with db() as con`(含 from database import db as 别名) 行范围内;
     core/database.py 自身(池实现)豁免
  A4 core/<域>/ 只 import 自己域的 repo 模块
  A5 app/routes 之间禁止互相 import
"""

import ast
import os
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent

DOMAINS = ("identity", "governance", "execution", "economy", "sharing")

SQL_PATTERNS = [
    re.compile(r"\bSELECT\b.{0,600}?\bFROM\b", re.DOTALL | re.IGNORECASE),
    re.compile(r"\bINSERT\s+INTO\b", re.IGNORECASE),
    re.compile(r"\bUPDATE\s+\w+\s+SET\b", re.IGNORECASE),
    re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE),
    re.compile(r"\bCREATE\s+(UNIQUE\s+)?(TABLE|INDEX)\b", re.IGNORECASE),
    re.compile(r"\bALTER\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bFOR\s+UPDATE\b", re.IGNORECASE),
]

# ── 白名单 (现状违例, 随批次清零; 批4 必须全空) ──
# A1: 批1 数据层落位时这些文件的 SQL 全部迁入 repo, 清空
A1_WHITELIST = {
    "scripts/migrate_sqlite_to_pg.py",  # 一次性迁移脚本, 存档性质
}
# A2: app 层事务边界暂留 (批1 服务在 core、编排仍在 routes); 批2 域服务
# 重建时事务下沉, 逐域清零
A2_WHITELIST = {
    "app/main.py", "app/routes/samelabs.py", "app/routes/auth_route.py",
    "app/routes/pages.py", "app/routes/earn.py", "app/routes/api.py",
    "app/routes/usertools.py", "app/routes/topup.py",
}
# A3: creem.py:81 = 审计🔴2(块外用连接), 批2 economy 重建时修复后删除
A3_WHITELIST = {"core/creem.py"}
# A4: 域目录建立前的平铺期空表
A4_WHITELIST = set()
# A5: 批2f(execution 服务收编)与批3(_ctx_base 下沉)清空
A5_WHITELIST = {
    "app/routes/samelabs.py:app.routes.pages",
    "app/routes/auth_route.py:app.routes.pages",
    "app/routes/proxy.py:app.routes.pages",
    "app/routes/apiv1.py:app.routes.api",
    "app/routes/pages.py:app.routes.usertools",
    "app/routes/api.py:app.routes.usertools",
    "app/routes/usertools.py:app.routes.api",
    "app/routes/topup.py:app.routes.earn",
    "app/routes/mcp.py:app.routes.apiv1",
    "app/routes/mcp.py:app.routes.api",
}


def _pyfiles(*roots):
    for root in roots:
        root = BASE / root
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            yield p


def _rel(p):
    return str(p.relative_to(BASE)).replace(os.sep, "/")


def _tree(p):
    return ast.parse(p.read_text(encoding="utf-8"))


def _db_aliases(tree):
    """收集 `from database import db[ as X]` 引入的名字 (含 'db' 本名)。"""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "database":
            for a in node.names:
                if a.name == "db":
                    names.add(a.asname or "db")
    return names


def _param_names(fn):
    args = fn.args
    out = {a.arg for a in (args.posonlyargs + args.args + args.kwonlyargs)}
    if args.vararg:
        out.add(args.vararg.arg)
    if args.kwarg:
        out.add(args.kwarg.arg)
    return out


class A1SqlOnlyInRepo(unittest.TestCase):
    def test_sql_confined_to_repo_and_schema(self):
        violations = []
        for p in _pyfiles("core", "app", "scripts"):
            rel = _rel(p)
            if rel in A1_WHITELIST:
                continue
            if rel == "core/database.py" or rel.startswith("core/repo/"):
                continue  # repo 本体与 schema 真相源, 放行
            tree = _tree(p)
            # docstring 节点集合(模块/类/函数首语句) — 排除
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    body = node.body
                    if (body and isinstance(body[0], ast.Expr)
                            and isinstance(body[0].value, ast.Constant)
                            and isinstance(body[0].value.value, str)):
                        docstrings.add(id(body[0].value))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and id(node) not in docstrings):
                    if any(rx.search(node.value) for rx in SQL_PATTERNS):
                        violations.append(f"{rel}:{node.lineno}")
                        break
        self.assertEqual(violations, [], "A1 违例(SQL 逃出 repo): " + ", ".join(violations))


class A2RoutesNoDb(unittest.TestCase):
    def test_routes_have_no_db_usage(self):
        violations = []
        for p in _pyfiles("app"):
            rel = _rel(p)
            if rel in A2_WHITELIST:
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\bwith\s+db\s*\(\s*\)", line) or re.search(r"\bcon\.execute\b", line):
                    violations.append(f"{rel}:{i}")
        self.assertEqual(violations, [], "A2 违例(routes 直连 db): " + ", ".join(violations))


class A3NoConnEscape(unittest.TestCase):
    def test_con_usage_inside_with_block(self):
        violations = []
        for p in _pyfiles("core", "app"):
            rel = _rel(p)
            if rel == "core/database.py" or rel in A3_WHITELIST:
                continue
            tree = _tree(p)
            aliases = _db_aliases(tree)
            with_ranges = [
                (n.lineno, n.end_lineno)
                for n in ast.walk(tree)
                if isinstance(n, ast.With)
                for item in n.items
                if (isinstance(item.context_expr, ast.Call)
                    and getattr(item.context_expr.func, "id", "") in aliases
                    and getattr(item.optional_vars, "id", "") == "con")
            ]
            # 函数级: con 作为参数 = 事务传递, 合法
            param_con_fns = {}
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if "con" in _param_names(node):
                        param_con_fns[node] = True

            def _in_param_fn(node):
                for fn in param_con_fns:
                    if fn.lineno <= node.lineno <= (fn.end_lineno or node.lineno):
                        return True
                return False

            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name) and node.value.id == "con"
                        and isinstance(node.ctx, ast.Load)):
                    if any(s <= node.lineno <= e for s, e in with_ranges):
                        continue
                    if _in_param_fn(node):
                        continue
                    violations.append(f"{rel}:{node.lineno}")
        self.assertEqual(violations, [], "A3 违例(con 逃逸 with 块): " + ", ".join(violations))


class A4DomainRepoIsolation(unittest.TestCase):
    def test_domains_import_own_repo_only(self):
        violations = []
        for dom in DOMAINS:
            dom_root = BASE / "core" / dom
            if not dom_root.exists():
                continue
            for p in dom_root.rglob("*.py"):
                if "__pycache__" in p.parts:
                    continue
                tree = _tree(p)
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.Import, ast.ImportFrom)):
                        continue
                    targets = ([a.name for a in node.names] if isinstance(node, ast.Import)
                               else [node.module or ""])
                    for t in targets:
                        if re.match(r"repo[\w.]*", t or ""):
                            # 只允许本域 repo 模块: repo/<dom>_*.py
                            parts = t.split(".")
                            if len(parts) >= 2 and parts[1].startswith(dom + "_"):
                                continue
                            violations.append(f"{_rel(p)}: import {t}")
        self.assertEqual(violations, [], "A4 违例(跨域 repo import): " + ", ".join(violations))


class A5NoCrossRouteImports(unittest.TestCase):
    def test_routes_do_not_import_each_other(self):
        violations = []
        for p in _pyfiles("app/routes"):
            rel = _rel(p)
            tree = _tree(p)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.routes."):
                    key = f"{rel}:{node.module}"
                    if key in A5_WHITELIST:
                        continue
                    violations.append(key)
        self.assertEqual(violations, [], "A5 违例(routes 互 import): " + ", ".join(violations))


if __name__ == "__main__":
    unittest.main(verbosity=2)
