# 重构基线 (批0, commit 0dd50b3 + 测试对齐)

采集时间: 2026-08-31 (UTC)

## E2E (全部通过, 输出存 /tmp/refactor_baseline/)
- tests/e2e_apiv1.sh — PASS (C3 五端点)
- tests/e2e_metering.sh — PASS (C1+C2 扣费/幂等/退费)
- tests/e2e_mcp.sh — PASS (断言已改成员制: 数据态目录可含公开用户工具)
- tests/e2e_samelabs.sh — PASS (C4)
- tests/acceptance/earn_e2e.sh — PASS (金额断言 300→30000 对齐币值改版)
- tests/acceptance/p2_e2e.sh — PASS (signup 奖励移除后: 直插 grant 种子再验扣费)

## 测试对齐修复 (只动测试, 产品代码零改动)
1. earn_e2e: 金额断言 300/100 → 30000/20000 (早期币值漏改)
2. e2e_mcp: len(tools)==11 → 成员断言+下限 (webhook_demo 公开上架)
3. p2_e2e: signup100 期望 → 移除后插种子 grant (signup 奖励 0827 已删)

## 页面 (未登录)
/ 200 /auth/* 200 /terms 200 /privacy 200 /agents 200 /tools 200
/account/* 302(登录重定向) /samelabs 302(门禁) /f/xxx 404

## 架构断言
tests/arch/test_layering.py 5 断言全绿(白名单=现状违例清单)

## 账本对账 (已知不匹配清单, 批2 economy 修复)
- ledger 870/876/882/888: meter-seed E2E 种子基线错 (历史)
- ledger 1432/1433+e2ewd/earn/p2seed 系: E2E 测试数据
- ledger 1437: consume after withdrawal #105 — 🔴#3 实例
  (withdraw balance_after 用全池 SUM, 双池口径违例; 修复后此类不再产生)
