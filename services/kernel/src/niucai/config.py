from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NIUCAI_", env_file=".env", extra="ignore", validate_assignment=True
    )
    database_url: str = "sqlite:///./niucai.db"
    api_token: SecretStr = SecretStr("")
    openrouter_api_key: SecretStr = SecretStr("")
    model_config_path: Path = Path("models.yaml")
    runtime: str = "deepagents"
    adapter: str = "disabled"
    workspace: Path = Path("./workspace")
    browser_cdp_url: str = "http://computer:9222"
    lease_seconds: int = 90
    poll_seconds: float = 1
    max_steps: int = 30
    context_chars: int = 24000
    tool_result_chars: int = 12000
    action_timeout: float = 30
    allow_shell: bool = False
    auto_create_schema: bool = False
