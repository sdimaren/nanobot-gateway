"""Configuration loading utilities."""

import json
import os
from pathlib import Path

from nanobot.config.schema import Config


# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    return Path.home() / ".nanobot" / "config.json"


def _load_dotenv(path: str | Path | None = None) -> None:
    """Parse a .env file and set values into os.environ (does not override existing vars).

    Path defaults to /root/.env inside Docker or NANOBOT_DOTENV_PATH env var.
    Set NANOBOT_DOTENV_PATH="" to disable.
    """
    dotenv_path = path or os.environ.get("NANOBOT_DOTENV_PATH", "/root/.env")
    if not dotenv_path or not os.path.exists(dotenv_path):
        return
    with open(dotenv_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def _inject_env_secrets(data: dict) -> None:
    """Merge environment-backed secrets into config dict (camelCase JSON keys).

    Covers the most common secrets so they never have to be stored in config.json:
      ANTHROPIC_API_KEY  → providers.anthropic.apiKey
      GEMINI_API_KEY     → providers.gemini.apiKey
      OPENAI_API_KEY     → providers.openai.apiKey
      OPENROUTER_API_KEY → providers.openrouter.apiKey
      TELEGRAM_TOKEN     → channels.telegram.token
      TELEGRAM_ALLOW_FROM (comma-separated) → channels.telegram.allowFrom
    """
    if not isinstance(data, dict):
        return

    prov = data.setdefault("providers", {})
    _secret_map = {
        "ANTHROPIC_API_KEY": "anthropic",
        "GEMINI_API_KEY": "gemini",
        "OPENAI_API_KEY": "openai",
        "OPENROUTER_API_KEY": "openrouter",
        "DEEPSEEK_API_KEY": "deepseek",
        "GROQ_API_KEY": "groq",
    }
    for env_key, provider_name in _secret_map.items():
        val = os.environ.get(env_key, "")
        if val:
            prov.setdefault(provider_name, {})["apiKey"] = val

    # Telegram
    tg_token = os.environ.get("TELEGRAM_TOKEN", "")
    tg_allow_raw = os.environ.get("TELEGRAM_ALLOW_FROM", "")
    tg_allow = [u.strip() for u in tg_allow_raw.split(",") if u.strip()]
    if tg_token or tg_allow:
        ch = data.setdefault("channels", {})
        tg = ch.setdefault("telegram", {})
        if tg_token:
            tg["token"] = tg_token
        if tg_allow:
            tg["allowFrom"] = tg_allow


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Automatically reads a .env file (path controlled by NANOBOT_DOTENV_PATH,
    defaulting to /root/.env inside Docker) and injects API keys and Telegram
    credentials into the config before validation. Secrets are never written
    to disk — they exist only in the process environment.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    # Source .env into environment (no-op if file absent or NANOBOT_DOTENV_PATH="")
    _load_dotenv()

    path = config_path or get_config_path()

    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate_config(data)
            _inject_env_secrets(data)
            return Config.model_validate(data)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")

    return Config()


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(by_alias=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")
    return data
