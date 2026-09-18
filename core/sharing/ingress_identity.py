"""sharing/ingress_identity — the single ingress request hash.

One helper for Web / REST v1 / MCP: the canonical content identity of a
side-effecting source ingress. Computed AFTER filename sanitize, content
sniff/resolve, and full body validation — never on raw transport bytes.
File array order is preserved. IP / user_id / request_ref are excluded:
the hash captures WHAT is being ingested, not who or under which key.
"""

import hashlib
import json as _json


def ingress_request_hash(kind: str, files: list[dict], context: dict) -> str:
    """SHA-256 hex of the canonical ingress payload.

    kind:   surface discriminator ('web_tool_source' | 'web_direct_share'
            | 'v1_source' | 'mcp_tool_source').
    files:  [{filename, content_type, size, sha256}, ...] — validated,
            sanitized, sniffed canonical content per file (body digest).
    context: surface-specific semantic context (e.g. tool_id for tool
            sources; delivery for direct share; {} for v1 raw source).
    """
    payload = {
        "kind": kind,
        "files": [{"filename": f["filename"],
                   "content_type": f["content_type"],
                   "size": int(f["size"]),
                   "sha256": f["sha256"]} for f in files],
        "context": dict(context),
    }
    blob = _json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
