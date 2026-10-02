from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Urgency = Literal["urgent", "soon", "normal", "none"]
ActionType = Literal["reply", "submit", "confirm", "approve", "attend", "schedule", "other", "none"]
Category = Literal["action", "information", "newsletter", "automated"]
TaskStatus = Literal["open", "done", "dismissed", "waiting"]


class EmailClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    requires_action: bool
    urgency: Urgency
    action_type: ActionType
    task: str | None = None
    deadline: date | None = None
    deadline_raw: str | None = None
    summary: str = Field(min_length=1, max_length=1000)
    category: Category
    reason: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_action_consistency(self) -> "EmailClassification":
        if self.requires_action and not self.task:
            raise ValueError("task is required when requires_action is true")
        if not self.requires_action and self.action_type != "none":
            raise ValueError("action_type must be none when requires_action is false")
        if not self.requires_action and self.urgency != "none":
            raise ValueError("urgency must be none when requires_action is false")
        return self


class EmailForClassification(BaseModel):
    source_id: str
    sender: str
    subject: str
    received_at: str
    body: str

