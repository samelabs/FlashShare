"""repo: catalog — platform_tools/pricing 数据访问 (the billing lock-order fix: 治理域专用).

规则: 函数=(con,...)->rows, 参数化, 无事务/无业务判断。
"""

from typing import Optional, Sequence


# ── platform_tools ──

def _like_token(tok: str) -> str:
    """One search token → a literal LIKE pattern: %tok% with LIKE
    wildcards (% _) and the escape character itself escaped — a query
    containing % or _ matches those characters literally."""
    tok = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{tok}%"


def _market_base(visibility_public: str, q: Optional[str],
                 curated: bool = True) -> tuple:
    """公开市场查询基座 (selection/search/pricing visibility): the ONE
    public-visibility predicate (enabled ∧ ¬delisted ∧ owner-alive) +
    public visibility, INNER JOIN pricing (缺行=未完成商业创建, 不上面;
    pricing.enabled=false 排除)。搜索 = 空白分词 AND: 每个词都须命中
    tool_key+label+desc(各语言) 之一, LIKE 通配符按字面匹配 (THE one
    search predicate — home selector / /tools market / MCP
    catalog_search 全部经由此处)。
    列表规则: 空查询 = 浏览, 只列运营推荐 (featured) 的工具, 按
    sort_order 排; 有查询 = 搜索, 命中全部公开工具。curated=False 仅供
    sitemap (全量公开集)。
    返回 (FROM+WHERE 片段, 参数)。"""
    where = ("pt.enabled AND NOT pt.delisted"
             " AND (pt.owner_id IS NULL OR u.status <> 'suspended')"
             " AND pt.visibility = %s")
    args: list = [visibility_public]
    tokens = (q or "").lower().split()
    if curated and not tokens:
        where += " AND pt.featured"
    for tok in tokens:
        like = _like_token(tok)
        where += (" AND (LOWER(pt.tool_key) LIKE %s"
                  " OR LOWER(pt.label_json)::jsonb::text LIKE %s"
                  " OR LOWER(pt.desc_json)::jsonb::text LIKE %s)")
        args += [like, like, like]
    base = (
        "FROM platform_tools pt "
        # LEFT JOIN: first-party rows carry owner_id NULL and must list
        "LEFT JOIN users u ON u.id = pt.owner_id "
        "JOIN pricing pr ON pr.tool_id = pt.tool_key "
        f"WHERE {where} AND pr.enabled")
    return base, args


def market_count(con, visibility_public: str, q: Optional[str],
                 curated: bool = True) -> int:
    """公开市场匹配总数 (COUNT 先行 — 供 clamp 后再取行, 未经 clamp 的
    page 不得进入 OFFSET)。curated 语义同 _market_base。"""
    base, args = _market_base(visibility_public, q, curated)
    return int(con.execute(f"SELECT COUNT(*) AS n {base}", args).fetchone()["n"])


def market_rows(con, visibility_public: str, q: Optional[str],
                limit: int, offset: int, curated: bool = True) -> list:
    """公开市场一页行 (offset 必须来自 clamp 后的 canonical page)。
    JOIN users: community 作者 handle 是卡片 identity contract 字段
    (official 行 owner 为空 → owner_handle NULL, 不伪造 author)。
    curated 语义同 _market_base。"""
    base, args = _market_base(visibility_public, q, curated)
    return con.execute(
        f"SELECT pt.*, pr.cost AS price_cost, "
        f"u.username AS owner_handle, "
        f"(SELECT COUNT(*) FROM tool_calls tc "
        f" WHERE tc.tool_id = pt.tool_key AND tc.status = 'ok') "
        f"AS ok_calls {base} "
        "ORDER BY pt.featured DESC, pt.sort_order, ok_calls DESC, pt.id "
        "LIMIT %s OFFSET %s",
        args + [limit, offset]).fetchall()


def public_sitemap_rows(con, visibility_public: str) -> list:
    """sitemap.xml 的公开目录行 (tool_key + 作者 handle + 声明 updated_at),
    无分页 — sitemap 就是全量公开集。可见性沿用 _market_base 单一谓词,
    此处不另立规则。"""
    base, args = _market_base(visibility_public, None, curated=False)
    return con.execute(
        f"SELECT pt.tool_key, pt.updated_at, u.username AS owner_handle "
        f"{base} ORDER BY pt.id", args).fetchall()


def by_tool_key(con, tool_key: str) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM platform_tools WHERE tool_key = %s", (tool_key,)).fetchone()


def tool_key_exists(con, tool_key: str) -> bool:
    return con.execute(
        "SELECT 1 FROM platform_tools WHERE tool_key = %s",
        (tool_key,)).fetchone() is not None


def all_tool_keys_by_age(con, include_suspended_owners: bool = False) -> list:
    """Every stored tool_key (all states), oldest row first — the input of
    the MCP tool-name projection (earlier rows keep their natural name).
    Suspended owners drop out unless the operator surface asks for all."""
    # NOTE: this feeds the MCP name map — naming, not visibility. A
    # delisted/suspended tool KEEPS its name slot so the map stays
    # stable (names are never reassigned); tools/list and resolve()
    # enforce visibility via the public predicate.
    join = "" if include_suspended_owners else (
        " LEFT JOIN users o ON o.id = platform_tools.owner_id"
        " WHERE (owner_id IS NULL OR o.status <> 'suspended')")
    # qualified: the join brings users.id into scope
    q = ("SELECT tool_key FROM platform_tools" + join
         + " ORDER BY platform_tools.id")
    return [r["tool_key"] for r in con.execute(q).fetchall()]


# ── THE public-visibility predicate (one source, every surface) ──
# A tool is publicly visible iff: developer enabled ∧ NOT operator
# delisted ∧ owner not suspended (first-party rows have no owner).
# resolve(), home/market lists, search, author page and MCP all build
# their SQL on this fragment — no surface may re-derive the rule.
_PUBLIC_VISIBLE = (" LEFT JOIN users o ON o.id = platform_tools.owner_id "
                   "WHERE platform_tools.enabled"
                   " AND NOT platform_tools.delisted"
                   " AND (platform_tools.owner_id IS NULL"
                   " OR o.status <> 'suspended') AND ")



def visible_to(con, visibility_public: str, viewer_id: Optional[int]) -> list:
    """呈现顺序 = 管理后台遴选: featured 优先, 再 sort_order, 再 id (注册序兜底)。
    owner_handle 随行选出 (社区卡作者槽; 官方行 NULL 不伪造)。"""
    vis_col = ("platform_tools.visibility = %s" if viewer_id is None
               else "(platform_tools.visibility = %s"
                    " OR platform_tools.owner_id = %s)")
    args = ([visibility_public] if viewer_id is None
            else [visibility_public, viewer_id])
    return con.execute(
        "SELECT platform_tools.*, o.username AS owner_handle"
        " FROM platform_tools"
        " LEFT JOIN users o ON o.id = platform_tools.owner_id"
        " WHERE platform_tools.enabled AND NOT platform_tools.delisted"
        " AND (platform_tools.owner_id IS NULL OR o.status <> 'suspended')"
        " AND " + vis_col +
        " ORDER BY platform_tools.featured DESC, platform_tools.sort_order,"
        " platform_tools.id",
        args).fetchall()


def version_digest(con) -> dict:
    return con.execute(
        """
        SELECT COUNT(*) AS n,
               COALESCE(MAX(updated_at), 0) AS mx,
               COALESCE(SUM(hashtext(tool_key)), 0) AS ck
        FROM platform_tools WHERE enabled
        """).fetchone()




def retire_absent_first_party(con, live_keys: Sequence[str], ts: int) -> None:
    # 空名单 = registry 加载失败 (import 错误/单测环境), 绝不能全灭线上工具
    if not live_keys:
        return
    # Retirement scope = CODE-SEEDED first-party rows only (execution
    # 'local'). Operator-created trusted HTTP tools share the bare-id
    # first-party namespace (owner_id IS NULL) but are live supply, not
    # registry mirrors — a restart must never disable them.
    con.execute(
        """
        UPDATE platform_tools SET enabled = FALSE, updated_at = %s
        WHERE owner_id IS NULL AND execution = 'local' AND enabled
          AND tool_key != ALL(%s)
        """,
        (ts, list(live_keys)))


def owner_rows_joined(con, user_id: int) -> list:
    return con.execute(
        """
        SELECT pt.*, u.username AS owner_name, p.cost, p.enabled AS priced
        FROM platform_tools pt
        JOIN users u ON u.id = pt.owner_id
        LEFT JOIN pricing p ON p.tool_id = pt.tool_key
        WHERE pt.owner_id = %s ORDER BY pt.id DESC
        """,
        (user_id,)).fetchall()


def lock_user_row(con, user_id: int) -> None:
    """对 owner 行 SELECT ... FOR UPDATE — 串行化同 owner 的并发 create,
    使 count-based cap (MAX_TOOLS_PER_USER) 在 READ COMMITTED 下成立。
    仅 user source adapter 使用 (policy 锁, 非 supply domain)。"""
    con.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))


def count_by_owner(con, user_id: int) -> int:
    return con.execute(
        "SELECT COUNT(*) AS n FROM platform_tools WHERE owner_id = %s",
        (user_id,)).fetchone()["n"]


def public_rows_by_owner(con, owner_id: int, visibility_public: str,
                         owner_suspended: bool = False) -> list:
    """用户页 (开发者公开面): 该作者的公开工具行, 同一可见性谓词与
    呈现序。停用作者 → 空列表 (作者页整体不可见, 调用方 404)。
    JOIN users: owner_handle 是卡片 identity contract 字段 — 与
    market_rows/visible_to 同形, 作者页卡片与其他三面同卡。"""
    if owner_suspended:
        return []
    return con.execute(
        "SELECT pt.*, u.username AS owner_handle "
        "FROM platform_tools pt "
        "JOIN users u ON u.id = pt.owner_id "
        "WHERE pt.owner_id = %s AND pt.enabled AND NOT pt.delisted"
        " AND pt.visibility = %s "
        "ORDER BY pt.featured DESC, pt.sort_order, pt.id",
        (owner_id, visibility_public)).fetchall()


def owner_row_by_key(con, tool_key: str, user_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM platform_tools WHERE tool_key = %s AND owner_id = %s",
        (tool_key, user_id)).fetchone()


def owner_row_for_update(con, tool_key: str, user_id: int) -> Optional[dict]:
    """Owner-scoped platform_tools 行锁读 — 用户 tool mutation 的 serialization
    boundary (并发 PATCH/DELETE 在此排队, snapshot 与 mutation 同事务)。"""
    return con.execute(
        "SELECT * FROM platform_tools WHERE tool_key = %s AND owner_id = %s"
        " FOR UPDATE", (tool_key, user_id)).fetchone()


def platform_row_for_update(con, tool_key: str,
                            owner_id_is_null: bool = True) -> Optional[dict]:
    """行锁读 platform_tools (operator delete/audit 的 serialization
    boundary — 并发 delete 在此排队, 后到者 commit 后重读得 not_found)。"""
    if owner_id_is_null:
        return con.execute(
            "SELECT * FROM platform_tools WHERE tool_key = %s"
            " AND owner_id IS NULL FOR UPDATE", (tool_key,)).fetchone()
    return con.execute(
        "SELECT * FROM platform_tools WHERE tool_key = %s FOR UPDATE",
        (tool_key,)).fetchone()


def tool_row_for_update(con, tool_key: str) -> Optional[dict]:
    """registered tool 的 pricing/presentation mutation 统一在
    platform_tools.tool_key 行锁上串行 (同一 tool 的三类 operator
    mutation 共用一个 serialization row; 无 lock table/version)。"""
    return con.execute(
        "SELECT * FROM platform_tools WHERE tool_key = %s FOR UPDATE",
        (tool_key,)).fetchone()


def set_tool_presentation(con, tool_key: str, featured: bool,
                          sort_order: int, ts: int) -> Optional[dict]:
    return con.execute(
        """
        UPDATE platform_tools SET featured=%s, sort_order=%s, updated_at=%s
        WHERE tool_key=%s RETURNING tool_key, featured, sort_order
        """, (featured, sort_order, ts, tool_key)).fetchone()



# ── 通用 tool row writers (阶段 B: 消费 tool_supply 序列化 row, 无业务验证) ──

_TOOL_COLUMNS = ("tool_key", "owner_id", "tool_id", "execution",
                 "label_json", "desc_json", "accept_json", "format_family",
                 "params_json", "files_min", "files_max",
                 "outputs_json",
                 "visibility", "endpoint_json", "enabled")


def insert_tool_row(con, row: dict) -> None:
    """通用 insert (user/operator 共用; row 已由 tool_supply 序列化)。"""
    cols = [c for c in _TOOL_COLUMNS if c in row]
    params = {c: row[c] for c in cols}
    params["ts"] = row["ts"]
    placeholders = ", ".join(f"%({c})s" for c in cols)
    con.execute(
        f"INSERT INTO platform_tools ({', '.join(cols)}, created_at, updated_at) "
        f"VALUES ({placeholders}, %(ts)s, %(ts)s)", params)


def upsert_seed_tool_row(con, row: dict) -> dict:
    """first-party seed upsert (保留行身份; visibility/enabled 语义由 SQL
    固化: seed 不动 visibility, enabled 反映声明)。新行以 featured 插入
    (首装即进浏览列表); 已有行的 featured/sort_order 属运营, seed 不覆盖。
    row 已序列化。"""
    cols = [c for c in _TOOL_COLUMNS if c in row and c not in ("visibility",)]
    params = {c: row[c] for c in cols}
    params["ts"] = row["ts"]
    placeholders = ", ".join(f"%({c})s" for c in cols)
    return con.execute(
        f"""
        INSERT INTO platform_tools ({', '.join(cols)}, visibility, featured, created_at, updated_at)
        VALUES ({placeholders}, 'public', TRUE, %(ts)s, %(ts)s)
        ON CONFLICT (tool_key) DO UPDATE SET
          label_json   = EXCLUDED.label_json,
          desc_json    = COALESCE(EXCLUDED.desc_json, platform_tools.desc_json),
          accept_json  = EXCLUDED.accept_json,
          format_family = EXCLUDED.format_family,
          params_json  = EXCLUDED.params_json,
          files_min = EXCLUDED.files_min,
          files_max = EXCLUDED.files_max,
          outputs_json = EXCLUDED.outputs_json,
          endpoint_json = EXCLUDED.endpoint_json,
          enabled      = EXCLUDED.enabled,
          updated_at   = EXCLUDED.updated_at
        RETURNING (xmax = 0) AS inserted
        """,
        params).fetchone()


def catalog_contract_rows(con) -> list:
    """全量 (tool_key, execution, accept_json, files_max) rows — 不过滤任何行
    (enabled/disabled、public/private/unlisted 全包含)。startup stored
    contract invariant (accept 单形状 + execution) 校验目标是全部存储行;
    解析/判定属于 governance 层。"""
    return con.execute(
        "SELECT tool_key, execution, accept_json, files_max"
        " FROM platform_tools").fetchall()


def update_tool_row(con, tool_key: str, sets: dict) -> None:
    """通用部分更新 (键=列名; sets 已由 tool_supply 映射)。"""
    if not sets:
        return
    assign = ", ".join(f"{k} = %s" for k in sets)
    con.execute(
        f"UPDATE platform_tools SET {assign} WHERE tool_key = %s",
        (*sets.values(), tool_key))


def delete_tool_row(con, row_id: int) -> None:
    con.execute("DELETE FROM platform_tools WHERE id = %s", (row_id,))


# ── pricing ──

def pricing_row_for_update(con, tool_id: str) -> Optional[dict]:
    """pricing 行锁读 — platform row 不存在但 pricing row 存在
    (retired tool) 时的 fallback serialization boundary。"""
    return con.execute(
        "SELECT * FROM pricing WHERE tool_id = %s FOR UPDATE",
        (tool_id,)).fetchone()


def pricing_row(con, tool_id: str) -> Optional[dict]:
    return con.execute(
        "SELECT tool_id, cost, enabled FROM pricing WHERE tool_id = %s",
        (tool_id,)).fetchone()


def upsert_pricing(con, tool_id: str, cost: int, enabled: bool,
                   ts: int, admin_user_id) -> dict:
    return con.execute(
        """
        INSERT INTO pricing (tool_id, cost, enabled, updated_at, updated_by)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (tool_id) DO UPDATE SET
          cost = EXCLUDED.cost, enabled = EXCLUDED.enabled,
          updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by
        RETURNING *
        """,
        (tool_id, cost, enabled, ts, admin_user_id)).fetchone()


def seed_pricing_row(con, tool_id: str, ts: int) -> int:
    cur = con.execute(
        """
        INSERT INTO pricing (tool_id, cost, enabled, updated_at)
        VALUES (%s, 0, TRUE, %s)
        ON CONFLICT (tool_id) DO NOTHING
        """,
        (tool_id, ts))
    return cur.rowcount


def all_pricing(con) -> list:
    return con.execute("SELECT * FROM pricing ORDER BY tool_id").fetchall()
