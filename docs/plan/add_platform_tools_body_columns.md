# Release migration: platform_tools 正文输入两列（body_format / body_placeholder_json）

**前置依赖**：`add_platform_tools_format_family.md`（tag 列资产）须先行执行。
本资产在其之后、同一部署窗口内执行。

**执行顺序（强制，一条线）：**
**只读预检 → ① 加两列 → ② 回填 → 切换前终检 → 部署首启。**

先迁移后部署：新代码的 startup `validate_catalog_rows` 即校验
`body_format` 互斥不变量，列不存在或未回填则服务起不来。旧代码
（1.6 工具链批）不读这两列，列先加上对运行中的旧服务零影响。

## 语义

- `body_format`：正文工具（files_min=files_max=0）的输入方式，
  `'text' | 'json'`；文件工具（files_max>0）恒 NULL。
- `body_placeholder_json`：正文输入占位（LocalizedText JSON）；仅正文
  工具可携带，文件工具恒 NULL。
- **回填归并规则（数据修复，非新机制）**：存量 `files_max = 0` 的行一律
  `body_format = 'text'`（通用规则；demo_shout 已随本批退役，其留存行
  同样适用）。回填后这些行即满足新启动不变量。
- **本批同时退役 demo_shout**（代码注册删除）；其行由
  `retire_absent_first_party` 翻 `enabled=false`，保留历史。

## ⚠ 部署前只读预检（两项均须为空才可部署；非空 → STOP 交 PM 裁定）

本批 P2 更换全部 http 工具的南向响应契约（Content-Type 结果解释取代
唯一 items 信封），存量 http 工具的行为会改变 —— 下列预检逐项给出裁定
所需事实，**“已经报告”不等于可以部署**。

```sql
-- (a) 非系统、files_max = 0 的行（附声明、参数、绑定）— 须为空
SELECT tool_key, label_json, params_json, endpoint_json
  FROM platform_tools
 WHERE owner_id IS NOT NULL AND files_max = 0;

-- (b) 全部 http 工具 — 非空即触发 PM 裁定（南向契约变更影响面）
SELECT tool_key, owner_id, trusted, files_min, files_max,
       accept_json, format_family, endpoint_json
  FROM platform_tools
 WHERE execution = 'http'
 ORDER BY owner_id NULLS FIRST, tool_key;
```

## ① 加两列 + ② 回填（幂等，同一停写窗口内执行）

停写窗口：② 之后旧服务不得再写入 `platform_tools`（社区发布/编辑、
运营建改）——窗口内新建的 `files_max=0` 行不会带 `body_format`，新代码
启动校验将拒绝它们；窗口内确需写入，写入后重跑 ②（幂等）。

```bash
cd <仓库检出根目录> && PYTHONPATH=core venv/bin/python - <<'PY'
from database import db
with db() as con:
    con.execute("""
        ALTER TABLE platform_tools
          ADD COLUMN IF NOT EXISTS body_format TEXT
            CONSTRAINT platform_tools_body_format_ck
            CHECK (body_format IS NULL OR body_format IN ('text','json'))
    """)
    con.execute("""
        ALTER TABLE platform_tools
          ADD COLUMN IF NOT EXISTS body_placeholder_json TEXT
    """)
    con.execute("""
        UPDATE platform_tools
           SET body_format = 'text'
         WHERE files_max = 0 AND body_format IS NULL
    """)
print("body columns added + backfilled")
PY
```

## 切换前终检（部署首启前，须为空）

```sql
SELECT tool_key FROM platform_tools
 WHERE files_max = 0
   AND (body_format IS NULL OR body_format NOT IN ('text','json'));
SELECT tool_key FROM platform_tools
 WHERE files_max > 0
   AND (body_format IS NOT NULL OR body_placeholder_json IS NOT NULL);
```

生产库在本批部署**前**执行**一次**；执行后删除本资产（仓库规则：
docs/plan/ 仅存放尚未执行的 release migration 资产）。
