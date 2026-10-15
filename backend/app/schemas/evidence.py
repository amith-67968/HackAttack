from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExtractedField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_name: str = Field(min_length=1, max_length=300)
    field_name: str = Field(min_length=1, max_length=100)
    # Keep nullable values required in the generated JSON schema so local models
    # cannot omit this key when reporting a found field.
    field_value: str | None = Field(max_length=2000)
    value_status: Literal["found", "not_found", "not_disclosed", "unverified", "conflicting"]
    supporting_excerpt: str = Field(max_length=1200)

    @model_validator(mode="after")
    def check_value_status(self):
        if self.value_status == "found" and (not self.field_value or not self.supporting_excerpt):
            raise ValueError("Found values require both a value and a supporting excerpt")
        if self.value_status != "found" and self.field_value is not None:
            raise ValueError("Unavailable or uncertain values must not contain an invented value")
        return self


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: list[ExtractedField] = Field(max_length=60)
