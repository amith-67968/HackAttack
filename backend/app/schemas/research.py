from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ResearchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location: str | None = Field(default=None, max_length=200)
    domain: str | None = Field(default=None, max_length=253)
    required_fields: list[str] = Field(default_factory=list, max_length=30)
    reference_urls: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("required_fields")
    @classmethod
    def clean_required_fields(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(field.strip() for field in value if field.strip()))

    @field_validator("reference_urls")
    @classmethod
    def clean_reference_urls(cls, value: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(url.strip() for url in value if url.strip()))
        for url in cleaned:
            if not url.startswith(("https://", "http://")):
                raise ValueError("Reference URLs must use HTTP or HTTPS")
        return cleaned


class InputMaterial(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1)

    @field_validator("content", mode="before")
    @classmethod
    def reject_oversized_text(cls, value: str) -> str:
        if isinstance(value, str) and len(value) > 50_000:
            raise ValueError("Input text must be no larger than 50 KB")
        return value

    @field_validator("filename")
    @classmethod
    def validate_text_filename(cls, value: str) -> str:
        from pathlib import PurePath

        name = value.strip()
        if PurePath(name).name != name or "/" in name or "\\" in name:
            raise ValueError("Filename must not contain a path")
        if PurePath(name).suffix.casefold() not in {".txt", ".md", ".markdown"}:
            raise ValueError("Only plain text and Markdown files can be ingested")
        return name

    @field_validator("content")
    @classmethod
    def validate_text_content(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 50_000:
            raise ValueError("Each input file must be no larger than 50 KB")
        if "\x00" in value:
            raise ValueError("Input material contains invalid binary data")
        return value


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=5, max_length=4000)
    output_type: Literal["auto", "spreadsheet", "email", "report"] = "auto"
    research_depth: Literal["standard", "deep"] = "standard"
    target_count: int | None = Field(default=None, ge=1, le=100)
    options: ResearchOptions = Field(default_factory=ResearchOptions)
    input_materials: list[InputMaterial] = Field(default_factory=list, max_length=4)
    failure_injection: Literal[
        "search_once", "page_timeout", "after_source_persist", "lost_finding_response",
        "restart_after_step", "exhaust_retries",
    ] | None = None

    @field_validator("query")
    @classmethod
    def clean_query(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 5:
            raise ValueError("Query must contain at least 5 non-whitespace characters")
        return value

    @model_validator(mode="after")
    def validate_total_material_size(self):
        if sum(len(item.content.encode("utf-8")) for item in self.input_materials) > 100_000:
            raise ValueError("Input materials must total no more than 100 KB")
        return self


class ResearchAccepted(BaseModel):
    task_id: str
    status: str
    events_url: str
