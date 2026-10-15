from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ExpectedResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=3, max_length=500)
    required_fields: list[str] = Field(default_factory=list, max_length=30)


class ResearchAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    action_type: Literal["web_search"]
    goal: str = Field(min_length=3, max_length=300)
    query: str = Field(min_length=3, max_length=500)
    required_tool: Literal["tavily_search"]
    expected_result: ExpectedResult
    verification_rule: str = Field(min_length=3, max_length=500)


class ResearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=2, max_length=300)
    summary: str = Field(min_length=3, max_length=1000)
    required_fields: list[str] = Field(min_length=1, max_length=30)
    actions: list[ResearchAction] = Field(min_length=1, max_length=5)

    @field_validator("required_fields")
    @classmethod
    def validate_fields(cls, value: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(field.strip() for field in value if field.strip()))
        if not cleaned:
            raise ValueError("At least one required field must be specified")
        return cleaned

    @field_validator("actions")
    @classmethod
    def unique_action_ids(cls, value: list[ResearchAction]) -> list[ResearchAction]:
        ids = [action.action_id for action in value]
        if len(ids) != len(set(ids)):
            raise ValueError("Action IDs must be unique")
        return value
