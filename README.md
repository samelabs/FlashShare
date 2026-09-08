# FlashShare · 518.PLUS

轻量级文件处理与分享平台。上传、处理、分享，一步到位。

## 功能

- 📁 文件上传（最大 50MB，74 种格式）
- 🔗 临时分享链接：可设有效期（1/3/7 天）与下载次数（1–200 次）
- 📱 每个文件自动生成二维码
- 🔧 内置文件工具：图片压缩 / 截图美化 / 隐私净化（剥元数据与 AI 水印）/ 图片转 PDF / PDF 合并 / 文档转 PDF / ICO 图标 / 裁白边 / ZIP 打包
- 🧩 开放工具链：第三方以 HTTP API 契约接入自己的工具，平台负责计量与分发
- 🤖 MCP 服务端（Streamable HTTP）：任意 MCP 客户端可调用全部工具，按次计费、失败自动退款
- 🛠️ REST API v1（Bearer Token）：程序与智能体接入
- 💰 双池账本：积分（消费）/ 收益（可提现）物理隔离
- 🌍 10 种语言界面，阿拉伯语 RTL 原生支持
- 👤 用户账号：本地注册或 X (Twitter) OAuth，支持绑定/解绑
- 📦 流式上传（64KB 分块），内存友好

## 环境要求

- Python 3.12+
- PostgreSQL
- [Gokapi](https://github.com/Forceuk/Gokapi)（文件存储）
- nginx（HTTPS 与反向代理，推荐）

## 快速开始

```bash
git clone https://github.com/samelabs/FlashShare.git
cd FlashShare
```

1. 配置环境变量（`/etc/flashshare.env` 或导出到 shell）：

```
DATABASE_URL=postgresql+psycopg://user:pass@127.0.0.1:5432/flashshare
FLASHSHARE_SESSION_SECRET=<随机长字符串>
FLASHSHARE_BASE_URL=https://your-domain
```

2. 启动：

```bash
./run.sh   # uvicorn app.main:app --workers 1
```

3. nginx 反代到 `127.0.0.1:8000`，`/d/`、`/dh/` 交给 Gokapi。

## API

### MCP（推荐，智能体接入）

端点 `https://your-domain/mcp`（Streamable HTTP），Bearer Token 认证。
在任意 MCP 客户端添加服务端 URL + Token 即可调用全部工具。

### REST API v1（程序接入）

| Method | Endpoint | 说明 |
|---|---|---|
| GET | `/api/v1/me` | 账户信息（id/用户名/余额） |
| GET | `/api/v1/tools` | 工具目录（含单价） |
| POST | `/api/v1/uploads` | 上传源文件 |
| POST | `/api/v1/execute` | 执行工具（client_ref 幂等） |
| GET | `/api/v1/uploads/{id}` | 按公开 id 获取产出 |

令牌在站内「API 令牌」页创建；`tools/call` 传 `client_ref` 重试返回原结果不重复扣费；失败自动退款。

## 部署架构

```
用户 → Cloudflare → nginx → uvicorn:8000 (FastAPI)
                                  ├── PostgreSQL（账本/用户/工具）
                                  └── Gokapi（文件存储）
```

## License

私有项目，保留所有权利。
