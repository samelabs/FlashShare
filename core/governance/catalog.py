"""Tool catalog — the data-state runtime for the tool registry (Phase 1).

register() in tools.py remains the CREATION format for first-party tools.
This module compiles those declarations into `platform_tools` rows and makes
the DATABASE the runtime source of truth for the catalog:

    startup:  load_all() → seed_from_registry() → rows exist
    catalog:  list_catalog() reads rows (pricing overlay here, one place)
    resolve:  resolve(tool_key, viewer) — the single routing entry for all
              three consumption surfaces (web grid, /api/v1, MCP)

Why: the platform thesis — new capabilities enter as DATA rows (user tools,
remote execution classes), never as code changes. First-party tools are just
seed rows with owner_id NULL and execution='local'.

Handler dispatch stays in tools.get_tool() for local tools (worker needs the
callable); catalog rows carry the DECLARATION, not the callable. resolve()
joins the two: DB row = contract, _registry = implementation for local class.
"""

import json

from database import db
from repo import catalog as catalog_repo
from utils import now

# Visibility ladder (data field, owner-settable — no human gate).
# Validation of stored values happens at write time (samelabs/remote and
# the future user-submission endpoint), not read time.
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
    ts = now()
    added = 0
    updated = 0
    for t in _registry:
        decl = {
            "tool_key": t["id"],
            "tool_id": t["id"],
            "owner_id": None,
            "execution": "local",
            "tool_type": t["type"],
            "label": t["label"],
            "desc": t.get("desc"),
            "icon": t["icon"],
            "accept": sorted(t["accept"]) if t.get("accept") else None,
            "multi": bool(t.get("multi", False)),
            "params": t.get("params"),
            "pass_through": bool(t.get("pass_through", False)),
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
    (formats.parse_stored_accept): SQL NULL = any / valid JSON non-empty
    canonical ext list. Anything else raises — never silently
    projected."""
    from governance.formats import parse_stored_accept
    canon = parse_stored_accept(raw)  # typed raise on invalid
    return sorted(canon) if canon else None


def validate_catalog_rows() -> None:
    """Startup invariant: every platform_tools row's stored accept must be
    the current contract (NULL = any, or non-empty canonical ext list).
    Violation aborts startup — catalog projection never guesses."""
    from governance.formats import parse_stored_accept
    with db() as con:
        rows = con.execute(
            "select tool_key, accept_json from platform_tools").fetchall()
    for r in rows:
        try:
            parse_stored_accept(r["accept_json"])  # SQL NULL = valid any
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"catalog invariant: tool {r['tool_key']} accept_json "
                f"violates the stored contract ({exc})") from None


def is_official_id(tool_id: str) -> bool:
    """Official/Community origin 单源判定: bare id = official (self-operated),
    namespaced 'owner/tool' = community。三面 (home/market/tool-page) 一律
    消费本判定, 禁止各自写字符串判断。"""
    return "/" not in (tool_id or "")


def row_to_item(row: dict, price: dict) -> dict:
    """platform_tools row + pricing row → canonical catalog item shape.

    row_to_item() defines THE canonical public catalog projection —
    list/search/owner/tool-page all consume this projection; no surface
    may rebuild a second public shape.
    """
    item = {
        "id": row["tool_key"],  # namespaced key is THE identity from now on
        "origin": "official" if is_official_id(row["tool_key"]) else "community",
        "type": row["tool_type"],
        "label": json.loads(row["label_json"]),
        "icon": row["icon"],
        # 4C-2: 公开 accept 一律投影成 canonical extension tokens
        # (stored parser 单点解释, 非法行 startup fail)。
        "accept": _canonical_accept(row["accept_json"]),
        "multi": bool(row["multi"]),
        "pass_through": bool(row.get("pass_through") or False),
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
    return item


def catalog_item(tool: dict) -> dict:
    """Resolve 成功的 platform_tools row → canonical catalog item.

    4A-fix5: Tool Page 单工具入口 — 与 list/search/owner 同一 projection
    (row_to_item) + pricing overlay。cost 来自 pricing 表 (single source),
    platform_tools 无 cost 列。

    pricing.enabled 不参与本函数访问判定 — 可访问性由 resolve() 决定,
    pricing.enabled 的现有访问语义不变 (仅 list/search/owner 列表隐藏)。
    """
    from governance.pricing import get_pricing
    price = get_pricing(tool["tool_key"])  # missing row → disabled (fail closed)
    return row_to_item(tool, price)


# ── Catalog (single source for web home, /api/v1/tools, MCP tools/list) ──

def search_catalog(q: str, viewer_id: int | None = None, limit: int = 30) -> list:
    """518-native tool search: prefix/substring match across name (all label
    langs) and tool key, self-operated + community in ONE result list.

    NOT a directory page (that's the MCP-marketplace pattern) — the home
    search box IS the tool selector: typing filters, clicking selects the
    active tool, the upload flow is unchanged. Community rows carry a
    provider badge so provenance is visible at a glance.
    """
    q = (q or "").strip().lower()
    if not q:
        return []
    like = f"%{q}%"
    with db() as con:
        rows = catalog_repo.search(con, VIS_PUBLIC, viewer_id, like, limit)
    out = []
    for row in rows:
        p = _price_of(row["tool_key"])
        if p is None:
            continue
        out.append(row_to_item(row, p))
    return out


def _price_of(tool_key: str) -> dict | None:
    """pricing overlay for one row: None = disabled/hidden."""
    from governance.pricing import get_pricing
    p = get_pricing(tool_key)
    if not p or not p["enabled"]:
        return None
    return p


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
    with db() as con:
        rows = catalog_repo.public_rows_by_owner(con, owner_id, VIS_PUBLIC)
    out = []
    for row in rows:
        p = prices.get(row["tool_key"]) or {"cost": 0, "enabled": False}  # missing → hidden
        if not p["enabled"]:
            continue
        out.append(row_to_item(row, p))
    return out


def list_catalog(viewer_id: int | None = None, include_remote: bool = True) -> list:
    """Pricing-overlaid catalog (row_to_item projection): disabled tools
    hidden, cost included.

    viewer_id: when given, the result additionally includes the viewer's
    own private/unlisted tools (owner surface). Public listing (web home
    for anonymous) passes None → public rows only.
    include_remote: False strips execution=http|mcp rows — the web home
    grid is local-only until the remote-tool UX ships; /api/v1 and MCP
    keep them (programmatic surfaces are the remote-first consumers).
    """
    from governance.pricing import list_pricing
    prices = {r["tool_id"]: r for r in list_pricing()}

    with db() as con:
        rows = catalog_repo.visible_to(con, VIS_PUBLIC, viewer_id)

    result = []
    for row in rows:
        if not include_remote and row["execution"] != "local":
            continue
        p = prices.get(row["tool_key"]) or {"cost": 0, "enabled": False}  # missing → hidden
        if not p["enabled"]:
            continue
        result.append(row_to_item(row, p))
    return result


MARKET_PAGE_SIZE = 24


def market_page(q: str | None = None, page: int = 1) -> dict:
    """公开市场页: 搜索 + 分页列表。顺序固定为 COUNT → total_pages →
    clamp page → OFFSET → rows — 未经 clamp 的 page 永不进入 SQL OFFSET
    (page=10^100 只是一次 COUNT, 不是巨大 OFFSET)。canonical projection
    仍走 row_to_item。

    返回 {items, total, page, total_pages}。页边界 canonical 行为:
    非法/非正数页 → 1; 超过末页 → 末页 (空目录时 total_pages=1)。
    排序与可见性语义同 visible_to: featured DESC, sort_order, id;
    pricing 缺行 = free + enabled, pricing.enabled=false 排除。
    Web 页码窗口 (ellipsis/page window) 属 presentation, 不在此层。
    """
    q = (q or "").strip() or None
    with db() as con:
        total = catalog_repo.market_count(con, VIS_PUBLIC, q)
    total_pages = max(1, -(-total // MARKET_PAGE_SIZE))
    canonical_page = min(max(page, 1), total_pages)
    with db() as con:
        rows = catalog_repo.market_rows(
            con, VIS_PUBLIC, q, MARKET_PAGE_SIZE,
            (canonical_page - 1) * MARKET_PAGE_SIZE)
    items = [row_to_item(row, {"cost": row["price_cost"], "enabled": True})
             for row in rows]
    return {"items": items, "total": total, "page": canonical_page,
            "total_pages": total_pages}


# ── resolve: the single routing entry for all surfaces ──

def resolve(tool_key: str, viewer_id: int | None = None) -> tuple:
    """Resolve a tool key to its full runtime declaration.

    Returns (tool, error) — exactly one is non-None.
      tool: dict with row fields + `handler` attached when execution='local'
            (the callable from tools._registry; worker dispatch needs it).
      error: 'not_found' | 'disabled' | 'forbidden'

    Key grammar: bare id ('beautify') = first-party; 'owner/tool' = user
    namespace (user tools land as data rows in Phase 3 — resolve() already
    speaks the grammar so no surface changes then).

    Visibility is enforced HERE so the three surfaces cannot diverge:
      public    → everyone
      unlisted  → everyone (callable, never listed)
      private   → owner only
    """
    with db() as con:
        row = catalog_repo.by_tool_key(con, tool_key)
    if not row:
        return None, "not_found"
    if not row["enabled"]:
        return None, "disabled"
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


def mcp_name(tool_key: str) -> str:
    """MCP tool-name projection: 'sun/super-resolve' → 'sun__super-resolve'
    (MCP name charset forbids '/'; bare first-party ids pass through)."""
    return tool_key.replace("/", "__")
