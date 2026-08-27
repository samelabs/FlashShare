# 前端第二刀规划：引入 Pico CSS v2 + CSS 技术债收口

> 2026-08-27 · 依据：全量检索（Pico v2.1.1 / Water.css / oat / keel / vitre / mmy-css 六候选实测对比）
> 用户裁定：引入轻量框架，规范技术债=提高未来效率

## 选型结论：Pico CSS v2.1.1（classless 变体起步）

**为什么是 Pico（检索实证，非拍脑袋）：**

| 维度 | Pico v2.1.1 | 落选原因（其他候选） |
|---|---|---|
| 成熟度 | 16.8k★ / 993 commits / v2.1.1 活跃 | oat、keel、vitre、mmy 全部 pre-1.0/2026 新项目——生产不赌 |
| 语义优先 | classless 变体+class-light 主线 | Water.css 成熟但**零组件**（modal/dropdown 还得手写=换汤不换药） |
| 组件覆盖 | accordion/card/dropdown/nav/modal/表单全套原生 | 518 现有自研组件全部有对应原生实现 |
| 零构建 | 单文件 link 即用 | Tailwind/Bootstrap 需构建或 23KB+ |
| 暗色模式 | prefers-color-scheme 自动 | 现状没有暗色 |
| 体积 | classless ~10KB（gz） | 现状 71KB，净减 60KB+ |
| RTL | v2 原生支持（阿语站已上线） | — |

**与现有范式矩阵核对**（用户铁律：新工具先判重叠）：
- 不重叠：Pico=语义基座（reset+元素+组件骨架），518 保留的是**业务组件层**（earn-grid/wd-row/topbar 布局）与**设计 token**（:root 变量改写 pico- 前缀变量即可换肤）
- 重叠面：自研的 modal/tab 样式 → Pico 原生 dialog/accordion 替代；16px input 地板 → Pico 表单默认达标

## 迁移策略（非大爆炸，分页渐进，每步回归）

**阶段 0（0.5 天）**：引入与隔离
- `static/vendor/pico.classless.min.css` 本地化（零外部请求，铁律）
- base.html 引入顺序：pico → style.css（style 后载=可覆盖任何 pico 默认）
- 全站视觉快照对比（关键 8 页），允许的最小 diff 记录在案

**阶段 1（1 天）**：元素层收口
- pico 接管：reset/排版/表格/表单/按钮基线/code/pre/暗色
- style.css 对应删：Reset 区/Typography 基线/acct-table 基础/表单全局/16px 地板
- 预计 -15KB

**阶段 2（1 天）**：组件层替换
- modal → `<dialog>`（pico 原生+现有 .modal 视觉保住：max-height/overflow-y 规则迁移）
- details/summary → pico accordion（接入契约折叠区/收益区块）
- dropdown 保持自研（pico dropdown 需要类，与 classless 路线冲突，且刚修稳）

**阶段 3（0.5 天）**：暗色模式
- pico 自动暗色 + :root token 对接（--pico-*-variables 覆盖）
- 图标/截图验收

**阶段 4**：清理与门禁固化
- style.css 预计 71KB → ~35KB（业务组件+布局 only）
- visual_probe 增加：pico 在场断言/dialog 语义断言/暗色 token 断言
- fe-structure 文档更新分层图：pico(基座) → style.css(业务) → pages/*.js(行为)

## 风险与守门

- **视觉回归**：每阶段 8 页快照对比+既有 7 套回归全绿才进下一阶段
- **CF 缓存**：vendor 文件带版本戳（同 style.css 机制）
- **不做**：不引 JS 框架、不做 SPA、不动 pages/*.js（上一刀刚稳定）

## 预估

总计 3 天，产出：71KB→~35KB、暗色模式免费获得、表单/弹层/手风琴原生化、未来新页面"写 HTML 即得体面样式"。
