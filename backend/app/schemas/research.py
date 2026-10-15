from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ResearchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location: str | None = Field(default=None, max_length=200)
    domain: str | None = Field(default=None, max_length=253)
    required_fields: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("required_fields")
    @classmethod
    def clean_required_fields(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(field.strip() for field in value if field.strip()))


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=5, max_length=4000)
    output_type: Literal["auto", "spreadsheet", "email", "report"] = "auto"
    research_depth: Literal["standard", "deep"] = "standard"
    target_count: int | None = Field(default=None, ge=1, le=100)
    options: ResearchOptions = Field(default_factory=ResearchOptions)

    @field_validator("query")
    @classmethod
    def clean_query(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 5:
            raise ValueError("Query must contain at least 5 non-whitespace characters")
        return value


class ResearchAccepted(BaseModel):
    task_id: str
    status: str
    events_url: str
