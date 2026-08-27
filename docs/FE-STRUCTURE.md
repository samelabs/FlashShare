# 前端结构治理·第一刀：JS 出模板

## 原则（用户数次指令的合并）
- 模板=纯 HTML+`<script src>`；JS 一律独立文件，全部过 node --check 进回归
- 不换范式、不加构建步骤（无 bundler）、零运行时行为变化
- Jinja 注入点一律改为数据注入：模板只写 `window.__PAGE__ = {...}`（唯一允许的 inline），JS 文件读它

## 提取清单（13 个文件，55KB）

| 模板 | 内联JS | Jinja 阻挡 | 处置 |
|---|---|---|---|
| account/tools.html | 242 行 | 无 | → static/pages/tools.js |
| account/earnings.html | 119 行 | 无 | → static/pages/earnings.js |
| detail.html | 130 行 | 3 处 tojson | 数据进 __PAGE__，JS 提取 |
| partials/footer.html | 62 行 | texts tojson | 同上（footer.js） |
| account/tokens.html | 67 行 | 3 处 texts | texts 键改走 window.t（键已存在），JS 提取 |
| account/profile.html | 7 行 | 1 处 texts | 同上 |
| base.html | 34 行 | user/icons/lang | 保留（全局引导，有注入依赖）——但加注释文档化结构 |
| auth/register.html | 19 行 | 无 | → pages/auth.js |
| agents.html | 13 行 | 无 | → pages/agents.js |
| samelabs/* 8 个 | 各 21-74 行 | 无 | → pages/samelabs/*.js |

## 执行序（每步：提取→node --check→页面断言→回归）
1. 无阻挡 6 文件（tools/earnings/agents/register + samelabs 纯JS）
2. tokens/profile（texts 键换 window.t）
3. detail/footer（__PAGE__ 数据注入模式）
4. base.html 结构文档化（block 嵌套+ctx 保留键注释）
5. i18n 辅助收敛：_msg/_errm 单份进 footer.js，删模板拷贝
6. 回归门禁固化：node --check 全部 pages/*.js + script 嵌套断言 + visual_probe

## 不做
- 不动 CSS（独立风险域，已有结构门禁）
- 不引 TS/bundler/框架
- 不合并 app.js（首页主逻辑，动则炸）
