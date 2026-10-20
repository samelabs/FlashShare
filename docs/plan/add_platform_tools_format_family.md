# Release migration: platform_tools.format_family（tag 列）

**执行顺序（强制，一条线）：**
**预检 → ① 加列 → ② 内置回填 → 停写窗口 → ③ 社区/运营回填（含 accept
物化）→ tag 预检 → 部署首启 → ④ 加 FK → 首启后复查。**

先迁移后部署（与 icon 列 DROP 资产方向相反）：新代码的 seed 启动即写
`format_family`，列不存在则 `UndefinedColumn` 起不来（PM 升级探针已
复现）。旧代码（v1.5）不读此列，列先加上对运行中的旧服务零影响。

**停写窗口（强制）**：③ 同时写 `format_family` **和** `accept_json`
（accept=NULL 的行物化全边界清单）——不是"只写新列"。因此 ③ 执行前
必须停掉旧服务对 `platform_tools` 的写入（社区发布/编辑、运营建改），
否则：预检之后旧服务新建的无 tag 行不会进 ③ 的回填集，新代码启动
校验将拒绝它们。窗口从 ③ 开始到新代码首启为止；窗口内若确需运维写
入，写入后必须重跑 ③（幂等）与下述终检。

**切换前终检（部署首启前，须为空）**：
```sql
SELECT tool_key FROM platform_tools
 WHERE files_max > 0 AND (format_family IS NULL OR accept_json IS NULL);
```

生产库在 1.6 工具链批部署**前**执行**一次**；执行后删除本资产
（仓库规则：docs/plan/ 仅存放尚未执行的 release migration 资产）。

所有脚本在**仓库主检出根目录**执行，`PYTHONPATH=core`（脚本内已自带
`sys.path` 处理的除外——统一用下面的调用形态）：

```bash
cd <仓库检出根目录> && PYTHONPATH=core venv/bin/python <脚本>
```

## 语义

- `format_family` = 作者声明的 tag（九键之一），文件工具必带、纯文本
  工具不带；图标 = tag 查表（`icons.tool_glyph`）。
- **回填归并规则**（数据修复，非新机制）：accept 单一场景 → 该场景
  tag；**多场景或无法归并 → `all`**（宽声明，作者可后续自行改）；
  accept 缺失的文件工具（仅运营表单可产生）→ **物化全边界清单 +
  `all`**（与 share/zip_pack 同口径：运营建的是"全部兼容"工具）。
  accept_json **解析失败必须报错**并打印 tool_key，不准吞。
- FK→tags：存量库在首启（tags 表由新代码 init_db 创建+播种）之后由
  本资产步骤④幂等补齐，保证与新建库 schema 一致。

## 预检（部署前；须为空 / 信息性见各条注释）

```sql
-- S5「零存量」依据: 任何含 / 的 key 都属未裁定存量（以下 4 条须为空）
SELECT tool_key FROM platform_tools WHERE tool_key LIKE '%/%';
SELECT user_id FROM tool_favorites WHERE tool_key LIKE '%/%';
SELECT tool_id FROM pricing WHERE tool_id LIKE '%/%';
SELECT id FROM tool_calls WHERE tool_id LIKE '%/%';
-- ③ 将物化的清单（信息性，不阻断）: share / zip_pack / 运营行
-- accept=NULL 的文件工具全部在 ③ 物化全边界清单 + all，逐行核对即可
SELECT tool_key FROM platform_tools WHERE files_max > 0 AND accept_json IS NULL;
-- 纯文本工具不得带 accept（须为空）
SELECT tool_key FROM platform_tools WHERE files_max = 0 AND accept_json IS NOT NULL;
```

## 步骤

```sql
-- ① 加列（幂等）
ALTER TABLE platform_tools ADD COLUMN IF NOT EXISTS format_family TEXT;
```

```bash
# ③ 全量回填（内置 + 社区 + 运营; 停写窗口内执行 — 同时写 format_family 与 accept_json）
# 内置 share/zip_pack (accept=NULL → 物化全边界+all) 与社区/运营行一并处理
cd <仓库检出根目录> && PYTHONPATH=core venv/bin/python - <<'PY'
import json, sys
from database import db
from governance.formats import FAMILIES, VOCAB_EXTS

FULL = len(VOCAB_EXTS)
with db() as con:
    rows = con.execute(
        "select tool_key, accept_json, files_max from platform_tools"
        " where format_family is null").fetchall()
    for r in rows:
        if int(r["files_max"]) == 0:
            continue  # 纯文本工具不带 tag
        if r["accept_json"] is None:
            # 运营表单 accept 选填产生的空清单文件工具:
            # 物化全边界 + all (与 share/zip_pack 同口径)
            con.execute(
                "update platform_tools set accept_json=%s, format_family='all'"
                " where tool_key=%s",
                (json.dumps(sorted(VOCAB_EXTS)), r["tool_key"]))
            print(f"materialized all-boundary: {r['tool_key']}")
            continue
        try:
            toks = set(json.loads(r["accept_json"]))
        except (TypeError, ValueError) as exc:
            raise SystemExit(
                f"accept_json unparsable for {r['tool_key']!r}: {exc}") from None
        hits = [k for k, f in FAMILIES.items() if toks & set(f.exts)]
        tag = hits[0] if (toks and len(toks) < FULL and len(hits) == 1) else "all"
        con.execute("update platform_tools set format_family=%s"
                    " where tool_key=%s", (tag, r["tool_key"]))
    print("backfill done")
PY
```

```sql
-- tag 预检（③ 之后、部署之前; 须为空）
SELECT tool_key FROM platform_tools
 WHERE files_max > 0 AND format_family IS NULL;
SELECT tool_key FROM platform_tools
 WHERE files_max = 0 AND format_family IS NOT NULL;
```

**→ 部署新代码并首启**（init_db 创建+播种 tags/formats；seed 落全部
内置 tag；`validate_catalog_rows` 启动校验全部行）。

```sql
-- ④ 补 FK（首启之后; DO 块幂等 — 重复执行不报错）
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conname = 'platform_tools_format_family_fkey') THEN
    ALTER TABLE platform_tools
      ADD CONSTRAINT platform_tools_format_family_fkey
      FOREIGN KEY (format_family) REFERENCES tags(key);
  END IF;
END $$;
```

```sql
-- 首启后复查: FK 存在 + 行数一致 + 违反单形状/记录数为零
SELECT count(*) FROM pg_constraint
 WHERE conname = 'platform_tools_format_family_fkey';   -- 1
SELECT count(*) FROM platform_tools
 WHERE (files_max > 0) <> (format_family IS NOT NULL)
    OR (files_max > 0 AND accept_json IS NULL);         -- 0
```
