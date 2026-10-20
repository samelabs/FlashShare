"""Tool catalog — the data-state runtime for the tool registry (Phase 1).

register() in tools.py remains the CREATION format for first-party tools.
This module compiles those declarations into `platform_tools` rows and makes
the DATABASE the runtime source of truth for the catalog:

    startup:  load_all() → seed_from_registry() → rows exist
    catalog:  list_catalog() reads rows (pricing overlay here, one place)
    resolve:  resolve(tool_key, viewer) — the single routing entry for all
              the consumption surfaces (web grid, MCP)

Why: the platform thesis — new capabilities enter as DATA rows (user tools,
remote execution classes), never as code changes. First-party tools are just
seed rows with owner_id NULL and execution='local'.

Handler dispatch stays in tools.get_tool() for local tools (worker needs the
callable); catalog rows carry the DECLARATION, not the callable. resolve()
joins the two: DB row = contract, _registry = implementation for local class.
"""

import json

from database import db
from icons import tool_glyph
from repo import catalog as catalog_repo
from utils import now

# Visibility ladder (data field, owner-settable — no human gate).
# Validation of stored values happens at write time (the operator
# samelabs surface and the user publishing/source adapter), not read time.
VIS_PRIVATE = "private"    # owner only
VIS_UNLISTED = "unlisted"  # callable when the key is known, never listed
VIS_PUBLIC = "public"      # grid + every caller's tools/list


# ── Seeding: register() declarations → platform_tools rows ──

def seed_from_registry() -> dict:
    """Sync first-party register() declarations into platform_tools.

    Idempotent upsert ON tool_key. First-party keys are bare ids ('beautify').
    Preserves row identity (id, created_at) and operator-managed fields that
    first-party seeding does not own (visibility stays 'public' for seeds,
    enabled reflects declaration on conflict — pricing.enabled remains the
    commerce switch, platform_tools.enabled is the registry switch).

    A tool removed from code keeps its row (enabled=false) so metering
    history stays interpretable — same convention as the pricing table.
    """
    # 阶段 B: registry → canonical declaration → tool_supply 单点 upsert。
    # 本函数不再解释 declaration 字段 (无 MIME shim / 手工 json.dumps)。
    from tools import _registry
    from governance.tool_supply import upsert_seed_tool
    from repo.catalog import by_tool_key
    ts = now()
    added = 0
    updated = 0
    for t in _registry:
        # 系统命名空间保护: 内置 key 撞上社区行 (username_toolid 形态下
        # 用户可组装出裸词形 key) = 身份占用, fail loud 绝不静默覆写
        with db() as con:
            existing = by_tool_key(con, t["id"])
        if existing is not None and existing["owner_id"] is not None:
            raise RuntimeError(
                f"seed collision: first-party key '{t['id']}' is held by a "
                f"community row (owner_id={existing['owner_id']}) — refusing "
                f"to overwrite; resolve the key conflict first")
        decl = {
            "tool_key": t["id"],
            "tool_id": t["id"],
            "owner_id": None,
            "execution": "local",
            "label": t["label"],
            "desc": t.get("desc"),
            "format_family": t.get("tag"),
            "body_format": t.get("body_format"),
            "body_placeholder": t.get("body_placeholder"),
            "inputs": {"files": {
                "min": int(t.get("files_min", 1)),
                "max": int(t.get("files_max", 1)),
                "accept": sorted(t["accept"]) if t.get("accept") else None}},
            "outputs": list(t.get("outputs") or ("file",)),
            "params": t.get("params"),
            "visibility": "public",
            "enabled": True,
        }
        if upsert_seed_tool(decl):
            added += 1
        else:
            updated += 1
    # Retired-in-code first-party tools: flip enabled off, keep the row
    with db() as con:
        catalog_repo.retire_absent_first_party(
            con, [t["id"] for t in _registry], ts)
    return {"added": added, "updated": updated}


def _canonical_accept(raw: str | None) -> list | None:
    """accept_json column → canonical extension token list (4C-2).

    Stored contract is parsed by THE single stored parser
    (formats.parse_stored_accept): 非空 canonical ext 列表; NULL 仅 =
    无文件位 (files_max=0)。Anything else raises — never silently
    projected."""
    from governance.formats import parse_stored_accept
    canon = parse_stored_accept(raw)  # typed raise on invalid
    return sorted(canon) if canon else None


def validate_catalog_rows() -> None:
    """Startup invariant: every platform_tools row's stored accept, tag,
    body columns and execution must be the current contract. accept 单形状:
    文件工具 (files_max>0) 必为非空 canonical ext 列表; 正文工具
    (files_max=0) 无文件位 → NULL。tag: 文件工具必带, 正文工具必不带。
    输入方式互斥: 正文工具 body_format 必为 'text'|'json' 且不带
    placeholder 之外的 body 列; 文件工具 body 两列恒 NULL。execution:
    canonical supply 词表 local|http。Violation aborts startup regardless
    of enabled/visibility — the catalog projection never guesses."""
    from governance.formats import parse_stored_accept
    from governance.tool_supply import (_EXECUTIONS,
                                        input_mode_violation)
    from repo.catalog import catalog_contract_rows
    with db() as con:
        rows = catalog_contract_rows(con)
    for r in rows:
        if r["execution"] not in _EXECUTIONS:
            raise RuntimeError(
                f"catalog invariant: tool {r['tool_key']} execution "
                f"'{r['execution']}' outside the canonical vocabulary "
                f"{_EXECUTIONS}")
        # 输入方式列互斥/搭配: 与 update_tool PATCH 终态校验共用同一
        # 判定函数 (supply 单点), 启动级语义 = RuntimeError。
        _code, _desc = input_mode_violation(
            r["tool_key"], r["files_max"], r["accept_json"],
            r["format_family"], r["body_format"],
            r["body_placeholder_json"])
        if _code:
            raise RuntimeError(f"catalog invariant: {_desc}")
        try:
            parse_stored_accept(r["accept_json"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"catalog invariant: tool {r['tool_key']} accept_json "
                f"violates the stored contract ({exc})") from None


def row_to_item(row: dict, price: dict, owner_verified: bool = False) -> dict:
    """platform_tools row + pricing row → canonical catalog item shape.

    row_to_item() defines THE canonical public catalog projection —
    list/search/owner/tool-page all consume this projection; no surface
    may rebuild a second public shape.

    owner_verified: the tool owner is a current effective member —
    computed by the caller via economy.subscription.effective_member_ids
    (the batch predicate shared with the access projection). First-party
    rows (owner NULL) always project False.
    """
    # 4C-2: 公开 accept 一律投影成 canonical extension tokens
    # (stored parser 单点解释, 非法行 startup fail)。
    accept = _canonical_accept(row["accept_json"])
    item = {
        "id": row["tool_key"],  # key 是唯一身份 (username_toolid | 裸词)
        # 系统工具判定 = owner 为空 (契约字段), 无字符串形状判定
        "origin": "official" if row["owner_id"] is None else "community",
        "label": json.loads(row["label_json"]),
        # 图标 = 声明 tag 的查表 (icons.tool_glyph 单点): 纯文本输入→text,
        # 九 tag 各一字形, all→cloud。无派生、无兜底。
        "icon": tool_glyph(row["files_max"], row["format_family"]),
        "accept": accept,
        "files_min": int(row["files_min"]),
        "files_max": int(row["files_max"]),
        # 正文工具输入方式 + 占位 (契约字段; 文件工具恒 None)
        "body_format": row.get("body_format"),
        "body_placeholder": (json.loads(row["body_placeholder_json"])
                             if row.get("body_placeholder_json") else None),
        "outputs": json.loads(row["outputs_json"]),
        "cost": int(price.get("cost") or 0),
    }
    # params 声明 (tool modes v3): 三面同构 — web 浮层/MCP inputSchema/详情同源
    item["params"] = json.loads(row["params_json"]) if row["params_json"] else None
    # 简介 (名称/简介/模式/使用): str|dict, 前端 resolveLabel 解析; 系统工具简介
    # 在 i18n {id}_desc — 前端 t() 双通道 (系统=i18n 键, 用户=desc_json)
    item["desc"] = json.loads(row["desc_json"]) if row.get("desc_json") else None
    # 遴选呈现 (后台管理): 推荐标记 + 排序
    item["featured"] = bool(row["featured"])
    item["sort_order"] = int(row["sort_order"])
    # community 作者 handle (identity contract; official 行为 NULL —
    # Official 不伪造 author)
    item["owner_handle"] = row.get("owner_handle")
    # 认证徽章数据: owner 当前为有效会员 (判定与 access projection 同谓词;
    # 官方工具 owner 为 NULL → False)
    item["owner_verified"] = bool(owner_verified) and row.get("owner_id") is not None
    return item


def _verified_owner_set(rows: list) -> set:
    """owner_id 列表 → 当前有效会员集合 (批量谓词单点, 一批一次查询)。"""
    from economy.subscription import effective_member_ids
    return effective_member_ids([r["owner_id"] for r in rows
                                 if r.get("owner_id") is not None])


def catalog_item(tool: dict) -> dict | None:
    """Resolve 成功的 platform_tools row → canonical catalog item.

    4A-fix5: Tool Page 单工具入口 — 与 list/search/owner 同一 projection
    (row_to_item) + pricing overlay。cost 来自 pricing 表 (single source),
    platform_tools 无 cost 列。

    Fail closed at the single point: a missing pricing row (tool not
    commercially created) or pricing.enabled=false yields None — the
    Direct Tool Page / MCP Tool Page must NOT render such a tool as a
    free tool. Access adjudication stays in resolve(); the commercial
    availability gate lives HERE, the one catalog authority (routes
    never re-implement it). List/search/market exclude such rows via
    their own pricing overlay — same rule, same module.
    """
    from governance.pricing import get_pricing
    price = get_pricing(tool["tool_key"])
    if not price["enabled"]:
        return None
    return row_to_item(tool, price,
                       owner_verified=tool.get("owner_id") in _verified_owner_set([tool]))


# ── Catalog (single source for web home, MCP tools/list) ──

def search_catalog(q: str, limit: int = 20) -> list:
    """Public tool search — THE one search surface behind the home
    selector (/api/tools/search), the /tools market page and MCP
    catalog_search: one predicate (repo._market_base: public visibility
    + pricing INNER JOIN), one ordering (featured → sort_order →
    successful calls → id, same as market_rows) and one projection
    (row_to_item). 查询按空白分词 AND, 每词命中 tool_key/label/desc
    之一, LIKE 通配符按字面匹配 (repo 单点); 空查询 = 浏览, 只列运营
    推荐 (featured) 的工具, 按 sort_order 取前 limit 个; 有查询 = 搜索,
    命中全部公开工具。搜索是公开发现面: 私有/unlisted 工具不进入
    (owner 面走 list_catalog / own surface)。
    """
    q = (q or "").strip() or None
    with db() as con:
        rows = catalog_repo.market_rows(con, VIS_PUBLIC, q, limit, 0)
    verified = _verified_owner_set(rows)
    return [row_to_item(row, {"cost": row["price_cost"], "enabled": True},
                        owner_verified=row["owner_id"] in verified)
            for row in rows]


def catalog_version() -> str:
    """Cheap change-detection for MCP clients: any insert/update/delete of
    an enabled row bumps this digest (count + max updated_at + key checksum).
    Agents compare versions to know when to re-list."""
    with db() as con:
        row = catalog_repo.version_digest(con)
    return f"v{row['n']}-{row['mx']}-{row['ck'] % 100000}"


def owner_public_tools(owner_id: int) -> list:
    """用户页 (开发者公开面): 该作者的公开 catalog items — 与三面同构
    (label/desc/params/cost 同 row_to_item 契约), 呈现序同全站。"""
    from governance.pricing import list_pricing
    prices = {r["tool_id"]: r for r in list_pricing()}
    from repo import users as users_repo
    with db() as con:
        owner_row = users_repo.by_id(con, owner_id)
        rows = catalog_repo.public_rows_by_owner(
            con, owner_id, VIS_PUBLIC,
            owner_suspended=bool(owner_row
                                 and owner_row["status"] == "suspended"))
    out = []
    verified = _verified_owner_set(rows)
    for row in rows:
        p = prices.get(row["tool_key"]) or {"cost": 0, "enabled": False}  # missing → hidden
        if not p["enabled"]:
            continue
        out.append(row_to_item(row, p,
                               owner_verified=row["owner_id"] in verified))
    return out


def list_catalog(viewer_id: int | None = None) -> list:
    """Pricing-overlaid catalog (row_to_item projection): disabled tools
    hidden, cost included.

    viewer_id: when given, the result additionally includes the viewer's
    own private/unlisted tools (owner surface). Public listing (web home
    for anonymous) passes None → public rows only.
    """
    from governance.pricing import list_pricing
    prices = {r["tool_id"]: r for r in list_pricing()}

    with db() as con:
        rows = catalog_repo.visible_to(con, VIS_PUBLIC, viewer_id)

    result = []
    verified = _verified_owner_set(rows)
    for row in rows:
        p = prices.get(row["tool_key"]) or {"cost": 0, "enabled": False}  # missing → hidden
        if not p["enabled"]:
            continue
        result.append(row_to_item(row, p,
                                  owner_verified=row["owner_id"] in verified))
    return result


MARKET_PAGE_SIZE = 24


def market_page(q: str | None = None, page: int = 1,
                curated: bool = True) -> dict:
    """公开市场页: 搜索 + 分页列表。顺序固定为 COUNT → total_pages →
    clamp page → OFFSET → rows — 未经 clamp 的 page 永不进入 SQL OFFSET
    (page=10^100 只是一次 COUNT, 不是巨大 OFFSET)。canonical projection
    仍走 row_to_item。

    返回 {items, total, page, total_pages}。页边界 canonical 行为:
    非法/非正数页 → 1; 超过末页 → 末页 (空目录时 total_pages=1)。
    列表规则同 search_catalog: 空查询只列推荐 (featured), 有查询命中
    全部公开工具; 排序 featured DESC, sort_order, 成功调用数, id;
    pricing INNER JOIN (缺行=未完成商业创建, 不上面), pricing.enabled=false
    排除。curated=False 关掉空查询的 featured 过滤 = 全量公开目录分页
    (MCP catalog_search 的枚举面, 与 sitemap 同源谓词)。
    Web 页码窗口 (ellipsis/page window) 属 presentation, 不在此层。
    """
    q = (q or "").strip() or None
    with db() as con:
        total = catalog_repo.market_count(con, VIS_PUBLIC, q, curated)
    total_pages = max(1, -(-total // MARKET_PAGE_SIZE))
    canonical_page = min(max(page, 1), total_pages)
    with db() as con:
        rows = catalog_repo.market_rows(
            con, VIS_PUBLIC, q, MARKET_PAGE_SIZE,
            (canonical_page - 1) * MARKET_PAGE_SIZE, curated)
    items = []
    verified = _verified_owner_set(rows)
    for row in rows:
        items.append(row_to_item(
            row, {"cost": row["price_cost"], "enabled": True},
            owner_verified=row["owner_id"] in verified))
    return {"items": items, "total": total, "page": canonical_page,
            "total_pages": total_pages}


# ── resolve: the single routing entry for all surfaces ──

def resolve(tool_key: str, viewer_id: int | None = None) -> tuple:
    """Resolve a tool key to its full runtime declaration.

    Returns (tool, error) — exactly one is non-None.
      tool: dict with row fields + `handler` attached when execution='local'
            (the callable from tools._registry; worker dispatch needs it).
      error: 'not_found' | 'disabled' | 'forbidden'

    Key grammar: 'username_toolid' (社区, owner 非空) | 裸词 (系统工具,
    owner 为空)。key 是唯一身份, 不解析不拆分 — owner 关系走 owner_id 列.

    Visibility is enforced HERE so the three surfaces cannot diverge:
      public    → everyone
      unlisted  → everyone (callable, never listed)
      private   → owner only
    """
    with db() as con:
        row = catalog_repo.by_tool_key(con, tool_key)
        # THE public-visibility predicate at the routing point (every
        # surface resolves through here): developer enabled ∧ NOT
        # operator delisted ∧ owner not suspended. Operator-delisted and
        # developer-disabled tools read as not_found on public surfaces
        # (no existence oracle); the owner's own view is the exception.
        if row is not None:
            from repo import users as users_repo
            if not row["enabled"] or row["delisted"]:
                if viewer_id is None or row["owner_id"] != viewer_id:
                    return None, "not_found"
            elif row["owner_id"] is not None:
                owner = users_repo.by_id(con, row["owner_id"])
                if owner is None or owner["status"] == "suspended":
                    return None, "not_found"
    if not row:
        return None, "not_found"
    if not row["enabled"] or row["delisted"]:
        # owner-only view of a disabled/delisted tool stays resolvable
        # so the owner can test/re-edit it
        if viewer_id is not None and row["owner_id"] == viewer_id:
            pass
        else:
            return None, "not_found"
    if row["visibility"] == VIS_PRIVATE:
        if viewer_id is None or row["owner_id"] != viewer_id:
            return None, "forbidden"

    tool = dict(row)
    if tool["execution"] == "local":
        from tools import get_tool as _get_local
        local = _get_local(tool["tool_id"])
        if local:
            tool["handler"] = local["handler"]
            # registry 运行时 accept 是 set — 此处 (authoring adapter) 转
            # canonical sorted list, 下游 validator list-only。
            tool["accept"] = sorted(local["accept"]) if local["accept"] else None
    return tool, None


# MCP 工具名 = tool_key 原文 (username_toolname / 裸词, 均为 MCP 合法
# 字符集)。无投影、无撞名分配 — key 唯一索引即名单射。名字解析 =
# by_tool_key 直查 (resolve() 内), 无逆映射函数。
