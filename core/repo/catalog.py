"""repo: catalog — platform_tools/pricing 数据访问 (批2a: 治理域专用).

规则: 函数=(con,...)->rows, 参数化, 无事务/无业务判断。
"""

from typing import Optional, Sequence


# ── platform_tools ──

def by_tool_key(con, tool_key: str) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM platform_tools WHERE tool_key = %s", (tool_key,)).fetchone()


def tool_key_exists(con, tool_key: str) -> bool:
    return con.execute(
        "SELECT 1 FROM platform_tools WHERE tool_key = %s",
        (tool_key,)).fetchone() is not None


def visible_to(con, visibility_public: str, viewer_id: Optional[int]) -> list:
    """呈现顺序 = 管理后台遴选: featured 优先, 再 sort_order, 再 id (注册序兜底)。"""
    if viewer_id is None:
        return con.execute(
            "SELECT * FROM platform_tools "
            "WHERE enabled AND visibility = %s "
            "ORDER BY featured DESC, sort_order, id",
            (visibility_public,)).fetchall()
    return con.execute(
        "SELECT * FROM platform_tools "
        "WHERE enabled AND (visibility = %s OR owner_id = %s) "
        "ORDER BY featured DESC, sort_order, id",
        (visibility_public, viewer_id)).fetchall()


def search(con, visibility_public: str, viewer_id: Optional[int],
           like: str, limit: int) -> list:
    if viewer_id is None:
        return con.execute(
            """
            SELECT * FROM platform_tools
            WHERE enabled AND visibility = %s
              AND (LOWER(tool_key) LIKE %s
                   OR LOWER(label_json)::jsonb::text LIKE %s)
            ORDER BY (owner_id IS NULL) DESC, id LIMIT %s
            """,
            (visibility_public, like, like, limit)).fetchall()
    return con.execute(
        """
        SELECT * FROM platform_tools
        WHERE enabled AND (visibility = %s OR owner_id = %s)
          AND (LOWER(tool_key) LIKE %s
               OR LOWER(label_json)::jsonb::text LIKE %s)
        ORDER BY (owner_id IS NULL) DESC, id LIMIT %s
        """,
        (visibility_public, viewer_id, like, like, limit)).fetchall()


def version_digest(con) -> dict:
    return con.execute(
        """
        SELECT COUNT(*) AS n,
               COALESCE(MAX(updated_at), 0) AS mx,
               COALESCE(SUM(hashtext(tool_key)), 0) AS ck
        FROM platform_tools WHERE enabled
        """).fetchone()


def upsert_first_party(con, tool_key: str, tool_type: str, label_json: str,
                       icon: str, accept_json: Optional[str],
                       presets_json: Optional[str], multi: bool,
                       ts: int, desc_json: Optional[str] = None) -> dict:
    """一方工具 seed (ON CONFLICT 更新声明字段, 保留行身份)。"""
    return con.execute(
        """
        INSERT INTO platform_tools
          (tool_key, owner_id, tool_id, execution, tool_type,
           label_json, desc_json, icon, accept_json, presets_json, multi,
           visibility, enabled, created_at, updated_at)
        VALUES (%s, NULL, %s, 'local', %s, %s, %s, %s, %s, %s, %s,
                'public', TRUE, %s, %s)
        ON CONFLICT (tool_key) DO UPDATE SET
          tool_type    = EXCLUDED.tool_type,
          label_json   = EXCLUDED.label_json,
          desc_json    = COALESCE(EXCLUDED.desc_json, platform_tools.desc_json),
          icon         = EXCLUDED.icon,
          accept_json  = EXCLUDED.accept_json,
          presets_json = EXCLUDED.presets_json,
          multi        = EXCLUDED.multi,
          updated_at   = EXCLUDED.updated_at
        RETURNING (xmax = 0) AS inserted
        """,
        (tool_key, tool_key, tool_type, label_json, desc_json, icon,
         accept_json, presets_json, multi, ts, ts)).fetchone()


def retire_absent_first_party(con, live_keys: Sequence[str], ts: int) -> None:
    con.execute(
        """
        UPDATE platform_tools SET enabled = FALSE, updated_at = %s
        WHERE owner_id IS NULL AND enabled AND tool_key != ALL(%s)
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


def count_by_owner(con, user_id: int) -> int:
    return con.execute(
        "SELECT COUNT(*) AS n FROM platform_tools WHERE owner_id = %s",
        (user_id,)).fetchone()["n"]


def public_rows_by_owner(con, owner_id: int, visibility_public: str) -> list:
    """用户页 (开发者公开面): 该作者的公开+启用工具行, 呈现序同全站。"""
    return con.execute(
        "SELECT * FROM platform_tools "
        "WHERE owner_id = %s AND enabled AND visibility = %s "
        "ORDER BY featured DESC, sort_order, id",
        (owner_id, visibility_public)).fetchall()


def owner_row_by_key(con, tool_key: str, user_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM platform_tools WHERE tool_key = %s AND owner_id = %s",
        (tool_key, user_id)).fetchone()


def id_of_platform_row(con, tool_key: str, owner_id_is_null: bool) -> Optional[dict]:
    if owner_id_is_null:
        return con.execute(
            "SELECT id FROM platform_tools WHERE tool_key = %s AND owner_id IS NULL",
            (tool_key,)).fetchone()
    return con.execute(
        "SELECT id FROM platform_tools WHERE tool_key = %s", (tool_key,)).fetchone()


def insert_remote_row(con, tool_key: str, tool_id: str, execution: str,
                      label_json: str, accept_json: Optional[str],
                      endpoint_json: str, ts: int) -> None:
    """operator 侧 remote 行 (owner NULL, public)。"""
    con.execute(
        """
        INSERT INTO platform_tools
           (tool_key, owner_id, tool_id, execution, tool_type,
            label_json, accept_json, presets_json, multi,
            visibility, endpoint_json, enabled, created_at, updated_at)
           VALUES (%s, NULL, %s, %s, 'produce', %s, %s, NULL, FALSE,
                   'public', %s, TRUE, %s, %s)
        """,
        (tool_key, tool_id, execution, label_json, accept_json,
         endpoint_json, ts, ts))


def insert_user_tool(con, tool_key: str, owner_id: int, tool_id: str,
                     execution: str, label_json: str,
                     accept_json: Optional[str], visibility: str,
                     endpoint_json: str, ts: int,
                     params_json: Optional[str] = None,
                     desc_json: Optional[str] = None) -> None:
    con.execute(
        """
        INSERT INTO platform_tools
           (tool_key, owner_id, tool_id, execution,
            label_json, accept_json, params_json, desc_json,
            visibility, endpoint_json, enabled, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s)
        """,
        (tool_key, owner_id, tool_id, execution, label_json, accept_json,
         params_json, desc_json, visibility, endpoint_json, ts, ts))


def set_tool_presentation(con, tool_key: str, featured: bool,
                          sort_order: int, ts: int) -> Optional[dict]:
    return con.execute(
        """
        UPDATE platform_tools SET featured=%s, sort_order=%s, updated_at=%s
        WHERE tool_key=%s RETURNING tool_key, featured, sort_order
        """, (featured, sort_order, ts, tool_key)).fetchone()


def update_fields(con, tool_key: str, sets: dict) -> None:
    """部分更新 (键=列名, 值=参数化)。列名由服务层白名单产生, 非用户输入。"""
    assign = ", ".join(f"{k} = %s" for k in sets)
    con.execute(
        f"UPDATE platform_tools SET {assign} WHERE tool_key = %s",
        (*sets.values(), tool_key))


def delete_tool_row(con, row_id: int) -> None:
    con.execute("DELETE FROM platform_tools WHERE id = %s", (row_id,))


def all_remote_joined(con) -> list:
    return con.execute(
        """
        SELECT pt.*, u.username AS owner_name FROM platform_tools pt
        LEFT JOIN users u ON u.id = pt.owner_id
        WHERE pt.execution = 'http' ORDER BY pt.id
        """).fetchall()


# ── pricing ──

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
