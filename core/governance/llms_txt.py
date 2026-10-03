"""/llms.txt — the plain-text guide for LLM agents (B7).

Everything dynamic (protocol versions, endpoint, tool catalog, prices)
is generated from the same code authorities the MCP surface uses;
only the stable prose is literal. text/plain; charset=utf-8, English
(language-independent of the site locale).
"""
from config import BASE_URL, PRODUCT_VERSION

LLMS_TOOLS_MAX = 50


def build_llms_txt() -> str:
    from governance.catalog import mcp_names, search_catalog
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
    a("> 518.PLUS — easily share files and AI capabilities. Use the "
      "tools on the web, or let an AI agent call them through MCP. To "
      "give a person a link to a file: call share; hand them "
      "result_url (one page for all files) — page_url is per file, "
      "download_url is a direct download.")
    a("")
    a("## MCP endpoint")
    a(f"- URL: {mcp_url} (Streamable HTTP: stateless HTTP POST, JSON-RPC 2.0)")
    a(f"- Protocol versions: {versions} (latest: {latest})")
    a("- Auth: Bearer API key, prefix fsk_. Connect without a key first: "
      "initialize and tools/list work anonymously, and the tool "
      "`register` (optional username) creates an account and returns "
      "its key — then send Authorization: "
      "Bearer <key>. Same over plain HTTP: POST /auth/mcp/register with "
      'an optional {"username": "..."} JSON body. Humans can also '
      "create keys in the web UI at /account/keys (Account → API keys)")
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
    a("# tools/list is your working set: first-party tools, your own tools "
      "and your favorites (paginate with params {\"cursor\":\"<nextCursor>\"}). "
      "The meta tool catalog_search finds any other tool by keyword; call it "
      "by name, and pin_tool / unpin_tool manage your favorites.")
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
    a("## Inputs (tools/call)")
    a("files[] items — 0..20 files per the tool's declared window "
      "(text-only tools take no files); each item is exactly one form:")
    try:
        from app.routes.mcp import MCP_MAX_BODY_BYTES
        body_cap = f"{MCP_MAX_BODY_BYTES // (1024 * 1024)} MB"
    except Exception:  # app layer unreachable in this process
        body_cap = "16 MB"
    a(f'1. Inline: {{"filename":"a.png","b64":"<base64>"}} — one /mcp '
      f"request body is capped at {body_cap}")
    a('2. https URL: {"url":"https://example.com/a.png"} — 518 fetches it '
      'server-side (public hosts only)')
    a('3. Reference: {"public_id":"...","source_token":"..."} to an '
      'existing upload of yours — a file item returned by an earlier call '
      'is referenced with just {"public_id":"..."} (chain tools without '
      're-uploading)')
    a("Plain-text input goes through the tool's params (text parameters "
      "accept up to 100,000 characters).")
    a("")
    a("## Results (items)")
    a("Every call returns an ORDERED list of items (at most 20):")
    a('  {"kind":"text","text":"..."}')
    a('  {"kind":"json","data":{...}}')
    a('  {"kind":"file","filename":"...","content_type":"...","size":N}')
    a("In tools/call the items arrive directly: structuredContent.items "
      "carries them with a download_url per file; small images also "
      "appear as image content blocks — unless the image bytes are "
      "identical to this call's input (a pass-through share never "
      "echoes your own bytes back). Every structuredContent response "
      "additionally carries a content text block holding the JSON "
      "serialization of that structuredContent (backward-compat form). "
      "result_url is a human-viewable page (/r/{id}).")
    a("")
    a("## Delivery (result retention)")
    from governance.delivery import EXPIRY_DAYS, DOWNLOAD_LIMITS
    a("Every tools/call accepts an optional `delivery` object that "
      "governs how long the result (and its file items) stays "
      "available and how many times files may be downloaded:")
    a('  {"delivery": {"expiry_days": 1, "allowed_downloads": 50}}')
    a(f"- expiry_days: how long the result lives (default 1). Legal "
      f"values: {list(EXPIRY_DAYS)}.")
    a(f"- allowed_downloads: total downloads allowed across the file "
      f"items (default 50). Legal values: {list(DOWNLOAD_LIMITS)}.")
    a("- Values outside your plan's allowance need a higher plan; an "
      "invalid value is returned as an isError result listing the "
      "legal values.")
    a("structuredContent.expires_at is the epoch second at which the "
      "result expires; after it, result_url and every file link 404.")
    a("")
    a("## Large files: two-stage upload (bytes never pass through the model)")
    a("For files too large to inline as base64 (the request body cap "
      f"is {body_cap}), call the free meta tool "
      "create_upload first, PUT the raw bytes to the returned one-time "
      "URL, then reference the result. Staged files are intermediates, "
      "not shares: use the PUT within 10 minutes; the stored copy "
      "lives at most 1 day and never appears in list_uploads (only "
      "its public_id/source_token reference matters).")
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
    a("## Your live files: list and delete (free meta tools)")
    a("- list_uploads: your still-live SHARED files (files you shared "
      "or that tool calls produced for you — create_upload staging is "
      "not listed), newest first, 20 per "
      'page, params {"offset":N}) — each entry: public_id, '
      "filename, size, content_type, expires_at, downloads_remaining, "
      "page_url, download_url.")
    a("- delete_upload: delete ONE of your own files by public_id — "
      "identical to deleting on the web (the /f/{public_id} page 404s "
      "immediately; stored bytes and mapping go together).")
    a("Example:")
    a("")
    a(f'curl -s {mcp_url} \\')
    a('  -H "Authorization: Bearer fsk_YOUR_API_KEY" \\')
    a('  -H "Content-Type: application/json" \\')
    a('  -d \'{"jsonrpc":"2.0","id":4,"method":"tools/call",'
      '"params":{"name":"list_uploads","arguments":{}}}\'')
    a("")
    a("## Share links")
    a(f"- /f/{{id}} is the human page; /f/{{id}}?download=1 is the direct "
      "download (302 redirect to the file).")
    a("- Download counts and expiry follow the `delivery` object of the "
      "tools/call that produced the file; anonymous download needs no "
      "token.")
    a("")
    a("## Billing")
    a("- Tool calls are metered in credits: 1000 credits = 1 USD. "
      "Each tool's description states its price (free or N "
      "credits/call); a call is charged when it runs — no confirmation "
      "step. Failed calls are refunded automatically.")
    a("- Out of credits → the call returns err_insufficient_credits; "
      f"the account owner logs in and tops up at {BASE_URL}/pricing, "
      "then the agent retries with the same client_ref (never charged "
      "twice). Free tools need no credits.")
    a("")
    a("## Limits")
    from config import MAX_UPLOAD_BYTES as _max_bytes
    from governance.entitlements import resolve_from_membership
    from utils import format_bytes
    _ent = resolve_from_membership(None)
    a(f"- Per file: {_max_bytes // 1024 // 1024} MB (every files[] item "
      "and every file a tool produces).")
    a(f"- Anonymous / free accounts: up to "
      f"{_ent['ingress_files_24h']} files and "
      f"{format_bytes(_ent['ingress_bytes_24h'])} per rolling 24 hours.")
    a(f"- Free delivery options: expiry_days "
      f"{list(_ent['expiry_days'])}, allowed_downloads "
      f"{list(_ent['download_limits'])}.")
    a("- Membership plans raise the file count, the 24-hour total and "
      "the delivery options — see /pricing.")
    a("")
    a("## Publish your own tool")
    a("Wrap any public HTTPS endpoint as a 518 tool: implement one JSON "
      "contract — source files arrive as one-time signed fetch URLs, "
      f"you answer with ordered text/json/file items — and submit it at "
      f"{BASE_URL}/account/tools/publish.")
    a("A new tool starts unlisted; after one successful run from its "
      "page it can go public, and then the web interface and every MCP "
      "agent can call it, earning you credits on each paid call.")
    a(f"Full guide — request/response examples, declarations, limits, "
      f"pricing and commission: {BASE_URL}/developers")
    a("")
    a("## Recommended tools (generated live; catalog_search finds every "
      "other public tool)")
    a("")
    names = mcp_names()
    for t in search_catalog("", limit=LLMS_TOOLS_MAX):
        name = names.get(t["id"], t["id"].replace("/", "__"))
        label = t.get("label") or {}
        desc = resolve_label(label, "en") or t["id"]
        cost = int(t.get("cost") or 0)
        price = "free" if cost == 0 else f"{cost} credits/call"
        author = (f" by @{t['owner_handle']}" if t.get("owner_handle")
                  else "")
        a(f"- {name}: {desc}{author} [{price}]")
    a("")
    a(f"(server: 518plus/{PRODUCT_VERSION}; this file is generated — "
      "do not edit by hand)")
    return "\n".join(lines) + "\n"
