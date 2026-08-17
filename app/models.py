"""Pydantic models for API request validation.

Pydantic v1 compatible (system package: python3-pydantic 1.10.14).
"""

from pydantic import BaseModel, Field
from typing import Dict, Any, List


class ToolExecuteRequest(BaseModel):
    tool_id: str = Field(..., min_length=1, max_length=64)
    public_id: str = Field(default="", max_length=48)
    # Typed lists so pydantic v1 actually enforces max_items
    public_ids: List[str] = Field(default_factory=list, max_items=20)
    # HMAC source token: binds an intermediate (gokapi-only) file to the
    # uploading user. Required when the file has no DB owner record.
    source_token: str = Field(default="", max_length=64)
    source_tokens: List[str] = Field(default_factory=list, max_items=20)
    params: Dict[str, Any] = Field(default_factory=dict)
    # Idempotency ref for programmatic callers (/api/v1, MCP). Web UI
    # omits it — each click is a fresh call. Replay with same ref
    # returns the recorded result without re-charging.
    client_ref: str = Field(default="", max_length=128)


class RenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
