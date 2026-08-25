# devlog — 改动摘要

2026-08-15 | HEAD | feat: img2pdf 图片转PDF工具上线 (第8个工具, Pillow原生PDF编码器零新依赖, 多文件→单PDF, EXIF转正, E2E+回归全绿)
- 2026-08-25 | EARN | MCP顶栏入口+发布工具命名统一+表单补全+收益提现闭环(earnings.py)
- 2026-08-25 | 4PT | 四点方向: 工具暴露/首页平台化(能力条+检索)/agents转MCP叙事/检索即选择
- 2026-08-25 | AUDIT2 | 全量审计: 修M10 output_url SSRF敞口+M11 MCP远程崩溃+M12 409+M18活动键族; i18n 13键; 验收脚本入库
- 2026-08-25 | CSSFIX | 弹层黑块+iOS聚焦放大: 16px基线84450ac起被注释截selector失效(全站级); modal移入scoped作用域
- 2026-08-25 | V2 | 我的工具页重做: 弹层+图标网格+收益展示+tab自适应(用户四病灶逐项修复)
- 2026-08-25 | P3 | C2C自助供给: /api/usertools+可见性阶梯+自测门+分账清算(30%佣金settings可调)+/account/tools页+MCP名投影修复
- 2026-08-25 | AUDIT | 两批全量审计: 修signup停用仍发100语义bug+i18n 4键10语+远程裸bytes白名单校验+后台两态/表单规范+窗口输入; 删死代码
- 2026-08-25 | 901741e | 远程执行类落地: http/mcp driver+签名URL拉取+SSRF守卫, samelabs远程工具管理页; CF对python-urllib UA拦截(1010)待WAF例外
- 2026-08-25 | b3937d9 | 积分活动模块化: activities表+/samelabs/activities管理页, 注册/每日发放数据驱动; 修复signup短路bug(新用户100积分未发放)
- 2026-08-25 | 889a229 | 工具链Phase1注册表数据化: platform_tools表+catalog.resolve统一路由链三通道一致; 附带修复MCP通道get_tool NameError死亡bug(213220a引入)
2026-08-15 | 18fc560 | feat: ZIP预览双形态上线 (上传前客户端CD解析预览 + 上传后浮层/详情页Range拉取列表, zip_preview.js共享模块, i18n 175键, E2E 4/4+回归全绿)
2026-08-15 | c9d5fff | feat: zip_pack 多文件ZIP打包工具上线 (zipfile stdlib, multi=True, 文件名去重, i18n 10语×2键, E2E 5项全过)
> 每次改动一条记录，新条目加在最上面。历史细节查 git log，此处不重复。

格式：`- 日期 | commit | 摘要`

---

- 2026-08-14 | 15eab49 | 文档清理：删 7 份过时文档（AUDIT-REPORT、DESIGN-rebuild/-architecture-upgrade/-production/-zip/-tools、ROADMAP）+ VISION（定位过时），DESIGN-architecture/tool-flow 修正死路由，新建本 devlog；AGENTS.md 修改后流程加 devlog 更新
- 2026-08-14 | 1fe41db | Pillow 工具链审查修复：spotlight 暗角回归（radial_gradient+colorize 精确径向）、aurora mesh 1/2→1/4 scale（979→292ms 视觉等价）、mint 对角渐变改 AFFINE 消角落楔块、dots 4x 超采样抗锯齿、PNG optimize 关闭（省 500ms/张）、privacy 1px 崩溃守卫。E2E 15/15
- 2026-08-14 | ffd0754 | 安全审计修复周期 100%：C-1 HMAC 源令牌+属主校验、C-2 XSS（|e/|tojson）、H-1..H-5（X-Real-IP/批量上限/登录节流/上限/i18n 映射）、M-1..M-7 死代码与规范化。回归 9/9 + E2E
- 2026-08-11 | a3dc177 | 审计收口：NotAllowedError 残留 + preset 切换清空文件
- 2026-08-10 | 8fc9b17 | 美化工具性能重构（后经 1fe41db 修正引入的质量回归）
- 2026-08-09 | e21bf08 | 工具链路稳定性：孤儿文件清理 + 内存安全；多文件额度绕过修复（b5fbbac）
- 2026-08-08 | 6ec907d | i18n 统一架构：1 数据源 + 1 引擎 + 1 遍历；历史记录逻辑归 footer.html
- 2026-08-07 | 9477682 | PDF 合并工具 + 多文件输入支持；e047148 PWA 原生体感基础设施
- 2026-08-07 | 552c4b6 | 截图美化预设重做：10 模式独立背景技术
- 2026-08-06 | b3b1055 | 日志系统：RotatingFileHandler 写文件 + journal
- 2026-08-06 | b0a88e6 | 品牌统一 518.PLUS + SEO/Meta 定稿；043f9c9 API 去掉 v1 前缀
- 2026-08-05 | 2ebe994 | FlashShare FastAPI 全栈初版（自 ThreadingHTTPServer 架构重建）

---

*更早历史：`git log --oneline`*
