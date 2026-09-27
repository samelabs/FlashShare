"""execution 域 — 工具执行闸门与编排 (the execution-domain split).

从 app/routes/api.py 收编的执行面核心:
  gate.py          计量闸门: replay/attempt → charge → 编排回调 → fact+分账/退费
  source.py        输入解析: public_id → upload (DB 记录或签名 token 的 gokapi 中间件)
  remote 编排      _execute_remote_and_share (remote.py 驱动器保持独立)

🔴 修复落位:
  🔴1 计费后校验失败 → 退费 (gate 的 fail 路径统一)
  🔴8 多文件上传中途失败 → 回滚已传 gokapi 件 (orchestrator)

事务红线: fact 行 + 分账 ledger 同一事务 (gate.finish)。
"""

class ExecutionError(Exception):
    """编排层错误: code = i18n error key。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
