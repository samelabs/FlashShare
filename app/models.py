"""Pydantic models for API request validation (pydantic v2).

执行请求形状已统一到 core/execution/dispatch._RawRequest (治理5);
本文件只保留仍有消费方的表单模型。
"""

from pydantic import BaseModel, Field


class RenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
