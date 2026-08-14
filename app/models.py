"""Pydantic models for API request validation.

Pydantic v1 compatible (system package: python3-pydantic 1.10.14).
"""

from pydantic import BaseModel, Field
from typing import Dict, Any


class ToolExecuteRequest(BaseModel):
    tool_id: str = Field(..., min_length=1, max_length=64)
    public_id: str = Field(default="", max_length=48)
    public_ids: list = Field(default_factory=list)
    params: Dict[str, Any] = Field(default_factory=dict)


class RenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
