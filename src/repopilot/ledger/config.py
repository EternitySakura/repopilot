from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class LedgerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REPOPILOT_LEDGER_", env_file=PROJECT_ROOT / ".env",
                                     extra="ignore", hide_input_in_errors=True)
    enabled: bool = False
    url: SecretStr = Field(default=SecretStr("sqlite:///" + (PROJECT_ROOT / "artifacts/ledger.sqlite3").as_posix()), exclude=True)
    source_url: SecretStr | None = Field(default=None, exclude=True)
