# devlog — 开发日志

> 格式：日期 | commit | 摘要。新条目加在最上面。**每次改动必须记录**（AGENTS.md 验收步骤的一部分）。

- 2026-08-28 | 2bcf858 | 废弃文档删除(docs/archive+devlog误删于本commit, 本条即恢复)
- 2026-08-28 | eb8ba21 | AGENTS.md全量重写对齐现实(PG/单源i18n/前端分层/服务器直开发); gen_i18n.py入tools/
- 2026-08-28 | b354322 | 语言切换终修: app.js借css版本戳→边缘旧文件永生; js_version独立戳×18模板
- 2026-08-28 | 0fa50ea | 栈溢出根因: app.js顶层function t遮蔽window.t无限递归(JS出模板后提升时序反转); const箭头修复; 客户端错误水龙头(/api/client-error)
- 2026-08-28 | ada15b2 | app.js加载自检(六元素缺失→红色横幅+中断, 版本错配不再静默半瘫)
- 2026-08-27 | a02d059 | applyI18n逐元素try/catch容错; 语言切换服务端全链实证
- 2026-08-27 | f81aefe | 暗雷清扫: script_head位丢失(__TOOLS__注入静默消失)+7页裸JS泄body+档名键误杀恢复(aurora/max等8键=beautify预设非皮肤)
- 2026-08-27 | d9ed788 | 三修: MCP胶囊文案统一MCP×10语+首页工具JS嵌套(block在script体内)+SSR语言占位5键
- 2026-08-27 | d83c406 | 顶栏MCP胶囊与logo拥挤: 窄屏三级收缩(820 gap下限/640紧凑态)
- 2026-08-27 | 3e38d54 | 全站六维审计: agents_ep_*误删恢复+homeShare残骸+17死键(五轮动态族验证)
- 2026-08-27 | 3284f06 | API令牌页: 吊销不展示+MCP卡删除+书签emoji清理
- 2026-08-27 | 0d2c0f2 | pico切conditional变体(classless全局重绘裸button/a事故); visual_sweep入库
- 2026-08-27 | cee95e2 | serviceWorker注册处置: 出模板+伪版本戳删除
- 2026-08-27 | 7b587f4 | MCP接入页: Hermes Skill区块移除(运营命令误入用户面)
- 2026-08-27 | 65de3b5 | 视觉系统清偿: 53死类+15死组合+5重复selector(两处块闭合自伤当场修复)
- 2026-08-27 | 983df00 | 前端结构治理第一刀: JS全部出模板(55KB→14文件); feguard门禁; i18n辅助收敛common.js
- 2026-08-27 | 393de09 | 收益页列表增长收口: 分页+封顶(LIMIT 100)
- 2026-08-27 | c0b3af6 | 注册赠送活动剥离(活动=机制参与裁定); daily-only; cost=0守卫恢复
- 2026-08-27 | 482040b | 错误提示统一解析器_errm(mytools_err_→err_→原文); app.js错误路径翻译
- 2026-08-27 | e097293 | 积分流水kind翻译(注册赠送等12种×10语)
- 2026-08-27 | b99ce40 | earn_none根因(全站i18n.js未加载)+下拉积分行样式+双池关系说明
