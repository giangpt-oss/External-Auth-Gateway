from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.reporting.schemas import TelegramScope


class PrepareRequest(TelegramScope):
    model_config = ConfigDict(extra='forbid')
    action: str = Field(min_length=1, max_length=80)
    payload: dict[str, Any] = Field(default_factory=dict)


class LookupRequest(TelegramScope):
    model_config = ConfigDict(extra='forbid')
    kind: str
    query: str = Field(default='', max_length=200)
    company_id: int | None = Field(default=None, gt=0)


class PreparedAction(BaseModel):
    status: str
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)
    snapshot: list[dict[str, Any]] = Field(default_factory=list)
    preview: dict[str, Any] = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class BusinessDraft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    business_action: str
    company_id: int | None = Field(default=None, gt=0)
    target_id: int | None = Field(default=None, gt=0)
    target_kind: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    lines: list[dict[str, Any]] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)


class ActionResult(BaseModel):
    status: str
    action: str
    record: dict[str, Any] = Field(default_factory=dict)
    message: str = ''
