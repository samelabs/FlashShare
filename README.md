<div align="center">

<img src="static/images/icon-192.png" width="72" alt="518.PLUS">

# 518.PLUS

**Tools and file sharing for AI agents and bots.**

Connect over MCP to process and share files. Publish your own tool and earn on every call.

[Website](https://518.plus) · [Connect your agent](https://518.plus/agents) · [Publish a tool](https://518.plus/account/tools/publish)

[![CI](https://github.com/samelabs/FlashShare/actions/workflows/ci.yml/badge.svg)](https://github.com/samelabs/FlashShare/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)

English · [简体中文](README.zh-CN.md)

</div>

## What it is

| | |
|---|---|
| **Agents & bots** | One MCP endpoint for every tool. Results come back directly, or as share links with an expiry and a download limit. |
| **People** | The same tools on the web and on mobile, with a result page to view and share. |
| **Tool developers** | Publish an HTTPS endpoint as a tool. It is listed immediately and earns on every paid call. |

## Connect your agent

Create an API key at [518.plus/account/keys](https://518.plus/account/keys), then add the MCP server.

Claude Code:

```bash
claude mcp add --transport http 518 https://518.plus/mcp --header "Authorization: Bearer fsk_YOUR_API_KEY"
```

Any MCP client (Streamable HTTP):

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

Files go in inline (base64), by HTTPS URL, or through a two-step upload for large files. Every call takes a `client_ref`, so retries never charge twice. The full agent guide is at [518.plus/llms.txt](https://518.plus/llms.txt).

## Publish a tool

A tool is an HTTPS endpoint. 518 sends it the input files and parameters:

```json
{
  "files": [{ "filename": "a.png", "content_type": "image/png", "size": 1024, "url": "https://..." }],
  "params": { "quality": 80 }
}
```

It returns an ordered list of results — text, JSON or files:

```json
{
  "items": [
    { "kind": "text", "text": "Compressed 1 image" },
    { "kind": "file", "filename": "a.png", "content_type": "image/png", "data_b64": "..." }
  ]
}
```

Set a price in credits (1,000 credits = US$1). Failed calls are refunded to the user automatically. Your share of each paid call goes to your earnings balance, which you can withdraw. The full declaration and execution contract is in [docs/publishing.md](docs/publishing.md).

## Self-hosting

Requirements: Python 3.12, PostgreSQL 16, [Gokapi](https://github.com/Forceu/Gokapi) v2.2.4, Nginx.

```bash
git clone https://github.com/samelabs/FlashShare.git && cd FlashShare
python3.12 -m venv venv && venv/bin/pip install -r requirements.txt
# set the environment variables listed in DEVELOPMENT.md, then:
venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

The database schema is created on first start. Configuration, deployment and tests are covered in [DEVELOPMENT.md](DEVELOPMENT.md).

## Contributing

Read [AGENTS.md](AGENTS.md) for the architecture contracts and [DEVELOPMENT.md](DEVELOPMENT.md) for the local test stack. Pull requests must pass CI.

## Security

Report vulnerabilities privately — see [SECURITY.md](SECURITY.md).

## License

[AGPL-3.0](LICENSE)
