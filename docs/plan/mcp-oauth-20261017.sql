-- One-time production migration for the MCP OAuth batch (batch/mcp-agent-surface-20261017).
-- Fresh databases get all of this from init_db; existing production
-- tables need the two ALTERs below. Run once at release, then remove
-- this file (repo rule: docs/plan/ holds only unexecuted migrations).
ALTER TABLE api_keys
  ADD COLUMN IF NOT EXISTS scopes TEXT NOT NULL
  DEFAULT 'tools:call files:read files:delete';
ALTER TABLE tool_calls
  ADD COLUMN IF NOT EXISTS oauth_client_id TEXT;
