"""Generate ignored, independent credentials for the local Docker acceptance stack."""

import secrets
from pathlib import Path

from dotenv import dotenv_values, set_key


def main():
    root = Path(__file__).resolve().parents[3]
    target = root / "build-cache/local-unified/.env"
    if target.exists():
        print("Existing isolated configuration retained.")
        return
    deployment = dotenv_values(root / "deploy/compose/.env")
    original = dotenv_values(root / ".env")
    values = {
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "NIUCAI_API_TOKEN": secrets.token_hex(32),
        "VNC_PW": secrets.token_hex(10),
        "NIUCAI_OPENAI_API_KEY": deployment.get("NIUCAI_OPENAI_API_KEY")
        or original.get("OPENAI_API_KEY", ""),
        "NIUCAI_OPENROUTER_API_KEY": deployment.get("NIUCAI_OPENROUTER_API_KEY", ""),
        "NIUCAI_OPENAI_API_BASE_URL": deployment.get("NIUCAI_OPENAI_API_BASE_URL")
        or original.get("OPENAI_API_BASE_URL", "https://openrouter.ai/api/v1"),
        "NIUCAI_ALLOW_SHELL": "false",
        "NIUCAI_MODEL_TIMEOUT": "120",
        "NIUCAI_RUNTIME_IDLE_TIMEOUT": "300",
    }
    if not (values["NIUCAI_OPENAI_API_KEY"] or values["NIUCAI_OPENROUTER_API_KEY"]):
        raise SystemExit("Configure a real model provider in the local .env first.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()
    for name, value in values.items():
        set_key(target, name, value or "", quote_mode="always")
    print("Independent test credentials generated; existing model configuration reused; secrets hidden.")


if __name__ == "__main__":
    main()
