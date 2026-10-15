from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ArtifactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_type: Literal["spreadsheet", "email", "report"]
    recipient: str | None = Field(default=None, max_length=320)


class ArtifactMetadata(BaseModel):
    id: str
    task_id: str
    artifact_type: str
    filename: str
    mime_type: str
    status: str
    preview: dict
    created_at: str | None = None
