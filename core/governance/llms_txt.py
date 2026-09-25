"""/llms.txt — the plain-text guide for LLM agents (B7).

Everything dynamic (protocol versions, endpoint, tool catalog, prices)
is generated from the same code authorities the MCP surface uses;
only the stable prose is literal. text/plain; charset=utf-8, English
(language-independent of the site locale).
"""
from config import BASE_URL, PRODUCT_VERSION


def build_llms_txt() -> str:
    from governance.catalog import list_catalog, mcp_names
    from governance.tool_params import resolve_label

    mcp_url = f"{BASE_URL}/mcp"
    try:
        from app.routes.mcp import SUPPORTED_VERSIONS
        versions = ", ".join(SUPPORTED_VERSIONS)
        latest = SUPPORTED_VERSIONS[0]
    except Exception:  # app layer unreachable in this process — fail loud text
        versions = latest = "(unavailable)"

    lines = []
    a = lines.append
    a("> 518.PLUS — a tools platform for people and AI agents. One catalog "
      "of tools: use them directly on the web, or let an AI agent call "
      "them through MCP.")
    a("")
    a("## MCP endpoint")
    a(f"- URL: {mcp_url} (Streamable HTTP: stateless HTTP POST, JSON-RPC 2.0)")
    a(f"- Protocol versions: {versions} (latest: {latest})")
    a("- Auth: Bearer API key, prefix fsk_ — create one in the web UI "
      "at /account/keys (Account → API keys); view it there any time")
    a("- Agent guide (this file): /llms.txt")
    a("")
    a("## No-install usage (curl)")
    a("MCP is plain HTTP POST JSON-RPC — no installation needed:")
    a("")
    a(f'curl -s {mcp_url} \\')
    a('  -H "Authorization: Bearer fsk_YOUR_API_KEY" \\')
    a('  -H "Content-Type: application/json" \\')
    a('  -d \'{"jsonrpc":"2.0","id":1,"method":"initialize",'
      '"params":{"protocolVersion":"' + latest + '"}}\'')
    a("")
    a(f'curl -s {mcp_url} \\')
    a('  -H "Authorization: Bearer fsk_YOUR_API_KEY" \\')
    a('  -H "Content-Type: application/json" \\')
    a('  -d \'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\'')
    a("")
    a("# paginate with params {\"cursor\":\"<nextCursor>\"}; the meta tool "
      "catalog_search finds tools by keyword")
    a("")
    a(f'curl -s {mcp_url} \\')
    a('  -H "Authorization: Bearer fsk_YOUR_API_KEY" \\')
    a('  -H "Content-Type: application/json" \\')
    a('  -d \'{"jsonrpc":"2.0","id":3,"method":"tools/call",'
      '"params":{"name":"share",')
    a('       "arguments":{"files":[{"filename":"a.png","b64":"..."}],')
    a('                    "client_ref":"my-stable-op-1"}}}\'')
    a("")
    a("client_ref is REQUIRED on every tools/call: a stable identity for "
      "one logical operation — reuse the same value to retry safely "
      "(replays return the original result, no double charge).")
    a("")
    a("## File inputs (tools/call files[] items — exactly one form each)")
    a('1. Inline: {"filename":"a.png","b64":"<base64>"}')
    a('2. https URL: {"url":"https://example.com/a.png"} — 518 fetches it '
      'server-side (public hosts only)')
    a('3. Reference: {"public_id":"...","source_token":"..."} to an '
      'existing upload of yours')
    a("")
    a("## Large files: two-stage upload (bytes never pass through the model)")
    a("For files too large to inline as base64, call the free meta tool "
      "create_upload first, PUT the raw bytes to the returned one-time "
      "URL, then reference the result:")
    a("")
    a(f'curl -s {mcp_url} \\')
    a('  -H "Authorization: Bearer fsk_YOUR_API_KEY" \\')
    a('  -H "Content-Type: application/json" \\')
    a('  -d \'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
      '"params":{"name":"create_upload",')
    a('       "arguments":{"filename":"big.png","size":5242880,')
    a('                    "client_ref":"stage-big"}}}\'')
    a('# → {"upload_url":".../mcp/upload/<ticket>","expires_at":...,'
      '"max_bytes":...}')
    a("")
    a('curl -s -X PUT --data-binary @big.png "<upload_url>"')
    a('# → {"public_id":"...","source_token":"..."}  (no Authorization '
      'header; the URL is the one-time credential, 10-minute expiry)')
    a("")
    a('Then tools/call any tool with files[]: '
      '[{"public_id":"...","source_token":"..."}].')
    a("")
    a("## Share links")
    a(f"- /f/{{id}} is the human page; /f/{{id}}?download=1 is the direct "
      "download (302 redirect to the file).")
    a("- Download counts and expiry follow the share settings made at "
      "upload time; anonymous download needs no token.")
    a("")
    a("## Billing")
    a("- Tool calls are metered in credits: 1000 credits = 1 USD. "
      "Failed calls are refunded automatically.")
    a("")
    a("## Tool catalog (public, generated live — same source as tools/list)")
    a("")
    names = mcp_names()
    for t in list_catalog(None):
        name = names.get(t["id"], t["id"].replace("/", "__"))
        label = t.get("label") or {}
        desc = resolve_label(label, "en") or t["id"]
        cost = int(t.get("cost") or 0)
        price = "free" if cost == 0 else f"{cost} credits/call"
        a(f"- {name}: {desc} [{price}]")
    a("")
    a(f"(server: 518plus/{PRODUCT_VERSION}; this file is generated — "
      "do not edit by hand)")
    return "\n".join(lines) + "\n"
