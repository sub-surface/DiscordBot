"""The bot's single source of configuration.

Precedence: environment (including `.env`) > `config.yaml` > the defaults below.
`.env` holds deployment details (tokens, backend, model, limits); `config.yaml`
holds bot behaviour (default persona, allowed channels).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    discord_token: str | None = None
    backend: str = "local"
    default_persona: str = "mochi"
    allowed_channels: tuple[str, ...] = ("shitpost", "sim-city", "little-st-james", "games")
    db_path: Path = ROOT / "history.db"
    personas_dir: Path = ROOT / "personas"

    temperature: float = 1.0
    top_p: float = 0.95
    top_k: int = 20

    local_base_url: str = "http://localhost:1234/v1"
    local_api_key: str = "lm-studio"
    local_model: str = "mimo-v2.6-distill-qwen-9b-mernik"
    local_context_tokens: int = 4096
    local_max_output_tokens: int = 768

    modal_app: str = "psychograph"
    modal_class: str = "MimoWorker"
    modal_model_id: str = "wepiqx/MiMo-V2.6-Distill-Qwen-9B-GGUF-MERNIK"
    modal_model_file: str = "MiMo-V2.6-Distill-Qwen-9B-MERNIK-5100.gguf"
    modal_context_tokens: int = 65536
    modal_max_output_tokens: int = 2048

    stockfish_path: str = ""
    stockfish_threads: int = 2
    stockfish_hash_mb: int = 128
    stockfish_move_time: float = 0.5


# Settings field -> environment variable, for fields configurable from `.env`.
ENV_NAMES = {
    "discord_token": "DISCORD_TOKEN",
    "backend": "LLM_BACKEND",
    "db_path": "DB_PATH",
    "temperature": "LLM_TEMPERATURE",
    "top_p": "LLM_TOP_P",
    "top_k": "LLM_TOP_K",
    "local_base_url": "LLM_BASE_URL",
    "local_api_key": "LLM_API_KEY",
    "local_model": "LLM_MODEL",
    "local_context_tokens": "LOCAL_CONTEXT_TOKENS",
    "local_max_output_tokens": "LOCAL_MAX_OUTPUT_TOKENS",
    "modal_model_id": "MODAL_MODEL_ID",
    "modal_model_file": "MODAL_MODEL_FILE",
    "modal_context_tokens": "MODAL_MAX_MODEL_LEN",
    "modal_max_output_tokens": "MODAL_MAX_OUTPUT_TOKENS",
    "stockfish_path": "STOCKFISH_PATH",
    "stockfish_threads": "STOCKFISH_THREADS",
    "stockfish_hash_mb": "STOCKFISH_HASH_MB",
    "stockfish_move_time": "STOCKFISH_MOVE_TIME",
}


def _coerce(value: object, default: object) -> object:
    if isinstance(default, bool):
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, Path):
        return Path(str(value)).expanduser()
    if isinstance(default, tuple):
        return tuple(str(item) for item in value) if isinstance(value, (list, tuple)) else tuple(str(value).split(","))
    if isinstance(default, (int, float)):
        return type(default)(value)
    return str(value).strip()


def load_settings(root: Path = ROOT) -> Settings:
    load_dotenv(root / ".env")
    config_path = root / "config.yaml"
    yaml_values = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    yaml_values = yaml_values or {}

    defaults = Settings()
    values: dict[str, object] = {}
    for item in fields(Settings):
        default = getattr(defaults, item.name)
        env_value = os.getenv(ENV_NAMES[item.name], "") if item.name in ENV_NAMES else ""
        if env_value.strip():
            values[item.name] = _coerce(env_value, default)
        elif item.name in yaml_values:
            values[item.name] = _coerce(yaml_values[item.name], default)
    values["backend"] = str(values.get("backend", defaults.backend)).lower()
    return Settings(**values)
