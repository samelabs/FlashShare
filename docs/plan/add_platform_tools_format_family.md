# Release migration: platform_tools.format_family（tag 列）

**执行顺序（强制）：先执行本迁移、后部署新代码。** 与 icon 列 DROP 资产
（先部署后 DROP）方向相反——新代码的 seed 启动即写 `format_family`，
列不存在则 `UndefinedColumn` 起不来（PM 升级探针已复现此失败）。
旧代码（v1.5）不读此列，列先加上对运行中的旧服务零影响。

生产库在 1.6 工具链批部署**前**执行**一次**；执行后删除本资产
（仓库规则：docs/plan/ 仅存放尚未执行的 release migration 资产）。

## 语义

- `format_family` = 作者声明的 tag（九键之一），文件工具必带、纯文本
  工具不带；图标 = tag 查表（`icons.tool_glyph`）。
- FK→tags 仅新建库自带（CREATE TABLE 内）；存量库本迁移不补 FK 约束
  （tags 表由新代码 init_db 首启创建+播种，存量库在迁移时刻尚无该表；
  值域合法性由 startup 不变量 `validate_catalog_rows` 承担，与
  execution 检查同口径 fail-loud）。

## 步骤

```sql
-- ① 加列（幂等）
ALTER TABLE platform_tools ADD COLUMN IF NOT EXISTS format_family TEXT;

-- ② 回填：内置工具（两个 all 型）
UPDATE platform_tools SET format_family = 'all'
 WHERE tool_key IN ('share', 'zip_pack') AND files_max > 0;

-- ③ 回填：社区文件工具 —— 按 accept 单族归并（旧派生规则做数据修复）；
--    多族/无法归并 → 'all'（宽声明，作者可后续自行改声明）；
--    纯文本工具 (files_max=0) 保持 NULL
UPDATE platform_tools t SET format_family = sub.tag
FROM (
  SELECT tool_key,
         CASE WHEN jsonb_array_length(accept_json::jsonb) >= 119 THEN 'all'
              ELSE (SELECT f.tag FROM formats_probe(f) LIMIT 1) END AS tag
  FROM platform_tools
) sub
WHERE false;  -- 占位说明：③ 见下方脚本（PG 无内嵌词表，走部署侧脚本）
```

③ 的精确回填由**部署侧脚本**执行（词表在代码/新表里，SQL 内不可内联）：

```bash
# 在新代码 checkout 上、部署前跑（只读库 + 写 format_family，不启动服务）:
venv/bin/python - <<'PY'
from database import db
from governance.formats import FAMILIES, VOCAB_EXTS
import json
FULL = len(VOCAB_EXTS)
with db() as con:
    rows = con.execute(
        "select tool_key, accept_json, files_max from platform_tools"
        " where format_family is null").fetchall()
    for r in rows:
        if int(r["files_max"]) == 0:
            continue  # 纯文本工具不带 tag
        tag = "all"
        try:
            toks = set(json.loads(r["accept_json"] or "[]"))
        except Exception:
            toks = set()
        hits = [k for k, f in FAMILIES.items() if toks & set(f.exts)]
        if toks and len(toks) < FULL and len(hits) == 1:
            tag = hits[0]
        con.execute("update platform_tools set format_family=%s"
                    " where tool_key=%s", (tag, r["tool_key"]))
    print("backfill done")
PY
```

（脚本的 FAMILIES 来自 formats.py 字面量种子——迁移时刻 tags 表尚未
由新代码播种，字面量与表内容在首装语义下一致。）

## 部署前只读预检（PM 指定清单，全部须为空）

```sql
SELECT tool_key FROM platform_tools WHERE files_max > 0 AND accept_json IS NULL;
SELECT tool_key FROM platform_tools WHERE files_max = 0 AND accept_json IS NOT NULL;
SELECT tool_key FROM platform_tools WHERE tool_key LIKE '%/%';   -- S5 零存量依据
SELECT user_id FROM tool_favorites WHERE tool_key LIKE '%/%';
SELECT tool_id FROM pricing WHERE tool_id LIKE '%/%';
SELECT id FROM tool_calls WHERE tool_id LIKE '%/%';
```

（tag 相关预检在本迁移②③之后跑：`files_max>0 AND format_family IS NULL` 须为空。）
