<div align="center">

<img src="brand/web/icon-192.png" width="72" alt="518.PLUS">

# 518.PLUS

**为 AI Agent 和 Bot 提供工具与文件分享。**

通过 MCP 处理和分享文件；发布你的工具，按调用获得收益。

[官网](https://518.plus) · [接入 Agent](https://518.plus/agents) · [发布工具](https://518.plus/account/tools/publish)

[![CI](https://github.com/samelabs/FlashShare/actions/workflows/ci.yml/badge.svg)](https://github.com/samelabs/FlashShare/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)

[English](README.md) · 简体中文

</div>

## 这是什么

| | |
|---|---|
| **Agent 与 Bot** | 一个 MCP 地址用上全部工具，也可以直接走 REST API（/v1/tools/{key}）调用任意工具。结果直接返回，或存成有有效期和下载次数的分享链接。 |
| **用户** | 网页和手机上使用同样的工具，结果页可查看、可分享。 |
| **工具开发者** | 把 HTTPS 端点发布为工具。新工具先不公开，本人成功运行一次后即可公开；平台不审核。每次付费调用都有收益。 |

## 接入你的 Agent

在 [518.plus/account/keys](https://518.plus/account/keys) 创建 API key，然后添加 MCP 服务。

Claude Code：

```bash
claude mcp add --transport http 518 https://518.plus/mcp --header "Authorization: Bearer fsk_YOUR_API_KEY"
```

任意 MCP 客户端（Streamable HTTP）：

```json
{
  "mcpServers": {
    "518": {
      "type": "http",
      "url": "https://518.plus/mcp",
      "headers": { "Authorization": "Bearer fsk_YOUR_API_KEY" }
    }
  }
}
```

只想用普通 HTTP？同一把 key 也适用于 REST 面：

```bash
curl -s -X POST https://518.plus/v1/tools/compress \
  -H "Authorization: Bearer fsk_YOUR_API_KEY" \
  -F "file=@input.png"
```

`GET /v1/tools` 列出目录；`Idempotency-Key` 请求头保证重试安全；
正文工具直接以请求体作为输入（Content-Type 按工具声明）。

打包插件或连接器？使用 OAuth 2.1——client id 即你的客户端元数据文档的 HTTPS 地址（CIMD）；发现入口：[518.plus/.well-known/oauth-authorization-server](https://518.plus/.well-known/oauth-authorization-server)。能力范围：`tools:call`、`files:read`、`files:delete`。

文件可内联（base64）、给 HTTPS 链接，大文件走两段式上传。每次调用带 `client_ref`，重试不会重复扣费。完整的 Agent 指南见 [518.plus/llms.txt](https://518.plus/llms.txt)。

## 发布工具

工具就是一个 HTTPS 端点。518 把输入文件和参数发给它：

```json
{
  "files": [{ "filename": "a.png", "content_type": "image/png", "size": 1024, "url": "https://..." }],
  "params": { "quality": 80 }
}
```

它返回有序的结果列表——文本、JSON 或文件：

```json
{
  "items": [
    { "kind": "text", "text": "已压缩 1 张图片" },
    { "kind": "file", "filename": "a.png", "content_type": "image/png", "data_b64": "..." }
  ]
}
```

以积分定价（1,000 积分 = 1 美元）。调用失败自动退款给用户。每次付费调用中你的分成计入收益余额，可以提现。完整的声明与执行规范见 [docs/publishing.md](docs/publishing.md)。

新工具发布后为不公开状态（不进公开市场）。作者本人成功运行一次该工具后，即可在「我的工具」中公开它——平台不审核工具。取消公开不受限制。

## 自部署

环境：Python 3.12、PostgreSQL 16、[Gokapi](https://github.com/Forceu/Gokapi) v2.2.4、Nginx。

```bash
git clone https://github.com/samelabs/FlashShare.git && cd FlashShare
python3.12 -m venv venv && venv/bin/pip install -r requirements.txt
# 按 DEVELOPMENT.md 设置环境变量，然后：
venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

数据库结构在首次启动时自动创建。配置、部署与测试见 [DEVELOPMENT.md](DEVELOPMENT.md)。

## 参与贡献

架构约定见 [AGENTS.md](AGENTS.md)，本地测试环境见 [DEVELOPMENT.md](DEVELOPMENT.md)。Pull request 须通过 CI。

## 安全

请私下报告漏洞，见 [SECURITY.md](SECURITY.md)。

## 许可证

[AGPL-3.0](LICENSE)
