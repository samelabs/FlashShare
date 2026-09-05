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
    "app/routes/usertools.py", "app/routes/topup.py", "app/web_auth.py",
}
# A3: economy.creem.py:81 = 审计🔴2(块外用连接), 批2 economy 重建时修复后删除
A3_WHITELIST = {"core/economy.creem.py"}
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


# 域 → 允许的 repo 模块显式映射 (repo 文件名与域非同名前缀时在此声明)
DOMAIN_REPO_ALLOW = {
    "identity": {"users", "sessions"},
    "governance": {"catalog", "orders", "ledger"},  # orders=settings KV; ledger=工具调用统计(治理投影)
    "execution": {"uploads", "ledger"},
    "economy": {"economy", "ledger", "orders"},
    "sharing": {"uploads"},
}


class A4DomainRepoIsolation(unittest.TestCase):
    def test_domains_import_own_repo_only(self):
        violations = []
        for dom in DOMAINS:
            dom_root = BASE / "core" / dom
            if not dom_root.exists():
                continue
            allowed = DOMAIN_REPO_ALLOW.get(dom, set())
            for p in dom_root.rglob("*.py"):
                if "__pycache__" in p.parts:
                    continue
                tree = _tree(p)
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.Import, ast.ImportFrom)):
                        continue
                    targets = []
                    if isinstance(node, ast.Import):
                        targets = [a.name for a in node.names]
                    elif (node.module or "").startswith("repo"):
                        targets = [node.module]
                    for t in targets:
                        if t == "repo" or (t or "").startswith("repo."):
                            parts = (t or "").split(".")
                            mod = parts[1] if len(parts) > 1 else "*"
                            if mod in allowed or mod == "*":
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


class A6CoreNoAppImports(unittest.TestCase):
    """core 不得 import app (分层单向: app → core)。"""

    def test_core_never_imports_app(self):
        violations = []
        for p in _pyfiles("core"):
            tree = _tree(p)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name.split(".")[0] == "app":
                            violations.append(f"{_rel(p)}: import {a.name}")
                elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "app":
                    violations.append(f"{_rel(p)}: from {node.module}")
        self.assertEqual(violations, [],
                         "A6 违例(core 反向依赖 app): " + ", ".join(violations))


class A7NoDuplicateDefs(unittest.TestCase):
    """同名函数跨文件分叉禁令 (白名单=真正的多态实现/测试)。"""

    # 已知合理重复 (模板 helper / 测试 / repo 参数化形状)
    A7_WHITELIST = {
        "_decode_file": None,  # mcp 域内
    }

    def test_no_cross_file_duplicate_defs(self):
        import collections
        defs = collections.defaultdict(list)
        for p in _pyfiles("core", "app"):
            rel = _rel(p)
            if rel.startswith("tests/"):
                continue
            tree = _tree(p)
            # 只扫模块级定义 (函数内闭包=页面局部惯例, 非跨文件分叉)
            top_nodes = list(tree.body)
            for node in top_nodes:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = node.name
                    if name.startswith("__"):
                        continue
                    # 域目录内同名为多态入口可接受; 跨文件同名 = 分叉候选
                    defs[name].append(rel)
        forks = {k: v for k, v in defs.items()
                 if len(v) > 1 and not k.startswith("_private_")}
        # 白名单过滤
        for k in list(forks):
            if k in ("client_ip",):
                continue  # dispatch 单点 + import 别名不算
        reported = {}
        for k, v in forks.items():
            uniq = sorted(set(v))
            # 服务×repo 同名对 = 分层同构 (服务=事务壳, repo=查询), 非分叉:
            # 同名且一方在 repo/ 一方在域服务目录 → 放行
            if len(uniq) == 2 and any(x.startswith("core/repo/") for x in uniq) \
                    and not all(x.startswith("core/repo/") for x in uniq):
                continue
            if len(uniq) > 1:
                reported[k] = uniq
        self.assertEqual(reported, {},
                         "A7 违例(跨文件同名定义=分叉): " +
                         "; ".join(f"{k}: {v}" for k, v in reported.items()))


class A8NoToolNameBranching(unittest.TestCase):
    """A8 (tool modes v3): 前端交互禁工具名特判 — 浮层/直选行为必须由
    数据 (presets/params) 推导, 与具体工具名无关。"""

    DQ = chr(34)   # "
    SQ = chr(39)   # '

    def test_no_tool_name_branching_in_frontend(self):
        import re
        q = "[\"'" + chr(39) + "]"
        pattern = re.compile(
            "toolId\\s*===?\\s*[" + chr(34) + chr(39) + "][a-z_]+["
            + chr(34) + chr(39) + "]|activeTool\\.id\\s*===?\\s*["
            + chr(34) + chr(39) + "][a-z_]+[" + chr(34) + chr(39) + "]")
        src = open("/opt/flashshare/static/app.js", encoding="utf-8").read()
        violations = pattern.findall(src)
        self.assertEqual(violations, [],
                         "A8 违例(前端工具名特判): " + ", ".join(violations))


class A9NoHardcodedDomain(unittest.TestCase):
    """A9 (域名解耦): 产品代码禁硬编码 518.plus — 域名只能经 config.BASE_URL
    (env FLASHSHARE_BASE_URL) 注入。站名叙事 (i18n 的 518.PLUS 文案) 与测试不算。"""

    def test_no_hardcoded_domain_in_product_code(self):
        import os
        offenders = []
        roots = ["app", "core", "static"]
        skip_dirs = {"__pycache__", "node_modules"}
        skip_files = {"i18n.py"}  # 站名叙事文案, 非域名
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in skip_dirs]
                for fn in filenames:
                    if not fn.endswith((".py", ".html", ".js", ".css")):
                        continue
                    if fn in skip_files or fn.endswith(".min.js"):
                        continue
                    p = os.path.join(dirpath, fn)
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        for lineno, line in enumerate(fh, 1):
                            if "518.plus" in line:
                                offenders.append(f"{p}:{lineno}: {line.strip()[:90]}")
        self.assertEqual(offenders, [],
                         "A9 违例(产品代码硬编码 518.plus): " + " | ".join(offenders[:8]))



class A10I18nJsInSync(unittest.TestCase):
    """A10 (i18n 双载体同步): core/i18n.py SERVER_I18N 与 static/i18n.js 是
    生成关系 (tools/gen_i18n.py)。加键不跑生成器 → live 切换显示裸键
    (0905 home_mcp_hint 实锤)。断言两载体键集全等。"""

    def test_i18n_js_matches_source(self):
        import json
        import re as _re
        import sys as _sys
        _sys.path.insert(0, "/opt/flashshare/core")
        from i18n import SERVER_I18N
        src = open("/opt/flashshare/static/i18n.js", encoding="utf-8").read()
        m = _re.search(r"window\.__I18N__ = (\{.*\});", src, _re.S)
        self.assertIsNotNone(m, "i18n.js 形态异常 (无 __I18N__ 赋值)")
        js = json.loads(m.group(1))
        diffs = []
        for lang in SERVER_I18N:
            a = set(SERVER_I18N[lang].keys())
            b = set(js.get(lang, {}).keys())
            missing = a - b   # 源有 js 无 = 裸键风险
            extra = b - a     # js 有 源无 = 陈旧键 (源删了没重生成)
            if missing or extra:
                diffs.append(f"{lang}: missing={sorted(missing)[:4]} extra={sorted(extra)[:4]}")
        self.assertEqual(diffs, [],
                         "A10 违例(改 i18n.py 未跑 tools/gen_i18n.py): " + " | ".join(diffs[:4]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
