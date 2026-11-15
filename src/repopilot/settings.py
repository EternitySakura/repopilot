from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from repopilot.repository.profiles import LOCAL_BUDGET


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REPOPILOT_", extra="ignore")

    max_iterations: int = Field(default=LOCAL_BUDGET.max_iterations, ge=1)
    max_tool_calls: int = Field(default=LOCAL_BUDGET.max_tool_calls, ge=1)
    max_edit_calls: int = Field(default=LOCAL_BUDGET.max_edit_calls, ge=1)
    max_test_calls: int = Field(default=LOCAL_BUDGET.max_test_calls, ge=1)
    max_no_progress: int = Field(default=LOCAL_BUDGET.max_no_progress, ge=1)
    tool_timeout_seconds: int = Field(default=LOCAL_BUDGET.tool_timeout_seconds, ge=1)
    max_run_seconds: int | None = Field(default=600, ge=1, le=3600)


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LLM_", env_file=Path(__file__).resolve().parents[2] / ".env",
        extra="ignore", hide_input_in_errors=True,
    )

    api_key: SecretStr = Field(exclude=True, min_length=1)
    model_id: Literal["deepseek-flash"] = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    max_calls: int = Field(default=8, ge=1, le=30)
    max_output_tokens: int = Field(default=1024, ge=128, le=4096)
    max_input_tokens: int = Field(default=24000, ge=4096, le=48000)
    max_total_tokens: int = Field(default=60000, ge=4096, le=200000)
    max_cost_usd: Decimal = Field(default=Decimal("0.05"), gt=0, le=1)
    timeout_seconds: int = Field(default=60, ge=1, le=120)

    @field_validator("base_url")
    @classmethod
    def official_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname != "api.deepseek.com"
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.port not in {None, 443} or parsed.path not in {"", "/", "/v1", "/v1/"}):
            raise ValueError("Only the official HTTPS DeepSeek endpoint is enabled")
        return value.rstrip("/")
