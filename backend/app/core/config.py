from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    backend_host: str = "127.0.0.1"
    backend_port: int = 8000
    frontend_origin: str = "http://localhost:5173"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:4b"
    ollama_timeout_seconds: float = 120.0
    ollama_num_ctx: int = Field(default=4096, ge=1024, le=32768)
    ollama_extraction_chunk_chars: int = Field(default=4000, ge=1000, le=12000)
    ollama_max_extraction_chunks: int = Field(default=4, ge=1, le=8)
    max_ollama_concurrency: int = Field(default=1, ge=1, le=4)
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    supabase_storage_bucket: str = "proofloop-artifacts"
    tavily_api_key: str = ""
    firecrawl_api_key: str = ""
    max_search_queries: int = Field(default=3, ge=1, le=5)
    max_pages_per_task: int = Field(default=10, ge=1, le=100)
    max_task_duration_seconds: int = Field(default=600, ge=10, le=3600)
    max_page_content_chars: int = Field(default=24000, ge=1000, le=100000)
    max_research_concurrency: int = Field(default=1, ge=1, le=4)
    provider_timeout_seconds: float = Field(default=30.0, gt=0, le=180)
    max_provider_attempts: int = Field(default=3, ge=1, le=5)
    retry_base_delay_seconds: float = Field(default=0.5, ge=0, le=30)
    retry_max_delay_seconds: float = Field(default=8, ge=0, le=120)
    max_sse_stream_seconds: int = Field(default=1800, ge=10, le=3600)
    failure_injection_enabled: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("ollama_base_url")
    @classmethod
    def trim_ollama_url(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
