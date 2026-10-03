import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(os.environ.get("INSIGHT_PROJECT_ROOT", Path(__file__).resolve().parents[2])).resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_prefix="INSIGHT_", extra="ignore"
    )
    project_root: Path = PROJECT_ROOT
    data_dir: Path = PROJECT_ROOT / "data"
    postgres_uri: SecretStr = SecretStr("")
    postgres_host: str = ""
    postgres_port: int = 5432
    postgres_user: str = "insight"
    postgres_database: str = "insight_agents"
    postgres_password: SecretStr = SecretStr("")
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_thinking: bool = False
    llm_review_thinking: bool = True
    llm_analysis_thinking: bool = True
    llm_review_effort: Literal["low", "high", "max"] = "low"
    review_max_output_tokens: int = Field(default=12000, ge=64, le=16000)
    embedding_base_url: str = ""
    embedding_api_key: SecretStr = SecretStr("")
    embedding_model: str = "bge-m3"
    embedding_dimensions: int = Field(default=1024, ge=1024, le=1024)
    model_timeout: float = Field(default=60, gt=0, le=300)
    max_model_calls: int = Field(default=24, ge=1, le=24)
    max_output_tokens: int = Field(default=3000, ge=64, le=16000)
    max_run_seconds: float = Field(default=300, gt=0)
    sql_timeout: float = Field(default=10, gt=0, le=60)
    # Historical setting name retained for env compatibility; this is the full result cap.
    sql_preview_rows: int = Field(default=20000, ge=1, le=20000)
    result_preview_rows: int = Field(default=50, ge=1, le=50)
    sql_attempts: int = Field(default=3, ge=1, le=3)
    summary_turns: int = 8
    summary_tokens: int = 8000
    recent_turns: int = 3
    summary_max_tokens: int = 1000
    cors_origins: list[str] = ["http://127.0.0.1:5174", "http://localhost:5174"]

    @model_validator(mode="after")
    def connection_parameters(self):
        if not self.postgres_uri.get_secret_value() and self.postgres_host:
            from psycopg.conninfo import make_conninfo
            self.postgres_uri = SecretStr(make_conninfo(host=self.postgres_host, port=self.postgres_port,
                user=self.postgres_user, dbname=self.postgres_database, password=self.postgres_password.get_secret_value()))
        return self

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)

    @property
    def sql_result_rows(self) -> int:
        return self.sql_preview_rows

    @property
    def scenario_root(self) -> Path:
        return self.project_root / "scenarios"

    @property
    def skill_root(self) -> Path:
        return self.project_root / "skills"
