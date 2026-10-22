from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NIUCAI_", env_file=".env", extra="ignore", validate_assignment=True
    )
    database_url: str = "sqlite:///./niucai.db"
    api_token: SecretStr = SecretStr("")
    openrouter_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    openai_api_base_url: str = "https://openrouter.ai/api/v1"
    model_config_path: Path = Path("models.yaml")
    runtime: Literal["pi", "deepagents", "structured"] = "pi"
    pi_storage: Path = Path("./runtime/pi")
    pi_entrypoint: Path = Path(__file__).resolve().parents[3] / "pi-runtime/dist/main.js"
    pi_node: str = "node"
    adapter: str = "disabled"
    workspace: Path = Path("./workspace")
    browser_cdp_url: str = "http://computer:9222"
    lease_seconds: int = 90
    poll_seconds: float = 1
    max_steps: int = 30
    max_model_turns: int = Field(default=100, ge=1, le=1000)
    context_chars: int = 24000
    tool_result_chars: int = 12000
    action_timeout: float = 30
    model_timeout: float = Field(default=120, gt=0)
    runtime_idle_timeout: float = Field(default=300, gt=0)
    allow_shell: bool = False
    # YOLO 总开关：开启后会话才能选择免审批；shell.exec 永远需要人工审批。
    yolo_enabled: bool = False
    # MCP 服务器由 Kernel 托管调用；stdio 服务器在 API/Worker 容器内启动，不是安全沙箱。
    mcp_enabled: bool = False
    mcp_timeout: float = Field(default=60, gt=0)
    auto_create_schema: bool = False
    remote_pi_enabled: bool = False
    multi_bot_enabled: bool = False
    worker_slots: int = Field(default=1, ge=1, le=32)
    handoff_max_hops: int = Field(default=4, ge=1, le=32)
    handoff_max_per_workflow: int = Field(default=32, ge=1, le=1000)
    workflow_max_model_turns: int = Field(default=200, ge=1)
    workflow_max_seconds: int = Field(default=3600, ge=1)
    handoff_timeout_seconds: int = Field(default=900, ge=1)
    routines_enabled: bool = False
    remote_lease_seconds: int = Field(default=30, ge=5, le=300)
    artifact_max_file_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    artifact_max_item_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    artifact_upload_ttl_seconds: int = Field(default=3600, ge=60)

    cookie_secure: bool = True
    computer_web_url: str = "/computer/vnc.html"
    computer_web_id: str = ""
    computer_web_upstream: str = ""
    computer_web_user: str = ""
    computer_web_password: SecretStr = SecretStr("")
