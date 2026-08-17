"""Centralised configuration for OpusFun.

Every secret and deployment-specific value is read from the environment
(optionally populated from a local ``.env`` file).  Nothing here is ever
hardcoded, and the loaded values are validated eagerly so that the bot fails
fast with a readable message instead of dying at runtime.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

#: Repository root (…/OpusFun)
BASE_DIR: Path = Path(__file__).resolve().parent.parent

# ``override=False`` keeps real environment variables (systemd, Docker, Heroku,
# Kubernetes secrets …) authoritative over the local .env file.
load_dotenv(BASE_DIR / ".env", override=False)


class ConfigError(RuntimeError):
    """Raised when the configuration is missing or malformed."""


def _get(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if value is not None:
        value = value.strip()
    if required and not value:
        raise ConfigError(
            f"Missing required environment variable: {name}. "
            "Copy .env.example to .env and fill it in."
        )
    return value or ""


def _get_int(name: str, default: int | None = None, *, required: bool = False) -> int:
    raw = _get(name, None if default is None else str(default), required=required)
    if not raw:
        return int(default or 0)
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - configuration guard
        raise ConfigError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


def _get_bool(name: str, default: bool = False) -> bool:
    raw = _get(name, "1" if default else "0").lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _get_id_list(name: str) -> List[int]:
    """Parse ``123,456 789`` style lists of Telegram IDs."""
    raw = _get(name)
    if not raw:
        return []
    parts = [chunk for chunk in raw.replace(",", " ").split() if chunk]
    ids: List[int] = []
    for part in parts:
        try:
            ids.append(int(part))
        except ValueError as exc:
            raise ConfigError(
                f"Environment variable {name} must contain integer user IDs; got {part!r}"
            ) from exc
    # Preserve order while removing duplicates.
    return list(dict.fromkeys(ids))


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable, validated runtime configuration."""

    # --- Telegram credentials -------------------------------------------------
    api_id: int
    api_hash: str
    bot_token: str

    # --- Database -------------------------------------------------------------
    mongo_uri: str
    mongo_db_name: str

    # --- Authorisation --------------------------------------------------------
    owner_ids: tuple[int, ...]

    # --- Branding / links -----------------------------------------------------
    bot_name: str
    start_img_url: str
    support_url: str
    update_url: str
    owner_url: str
    developer_url: str

    # --- Behaviour tuning -----------------------------------------------------
    couple_cooldown_hours: int
    broadcast_concurrency: int
    broadcast_sleep: float
    media_cache_ttl: int
    log_level: str
    log_file: str
    workers: int
    default_language: str

    # --- Paths ----------------------------------------------------------------
    base_dir: Path = field(default=BASE_DIR)

    # ------------------------------------------------------------------ helpers
    @property
    def assets_dir(self) -> Path:
        return self.base_dir / "assist"

    @property
    def locales_dir(self) -> Path:
        return self.base_dir / "locales"

    @property
    def couple_cooldown_seconds(self) -> int:
        return self.couple_cooldown_hours * 3600

    def is_owner(self, user_id: int | None) -> bool:
        """Single source of truth for owner authorisation."""
        return user_id is not None and int(user_id) in self.owner_ids

    @classmethod
    def load(cls) -> "Config":
        owners = _get_id_list("OWNER_IDS")
        if not owners:
            raise ConfigError(
                "OWNER_IDS is empty. Set at least one Telegram user ID, e.g. "
                "OWNER_IDS=123456789,987654321"
            )

        api_hash = _get("API_HASH", required=True)
        bot_token = _get("BOT_TOKEN", required=True)
        if ":" not in bot_token:
            raise ConfigError("BOT_TOKEN does not look like a valid bot token.")

        cooldown_hours = _get_int("COUPLE_COOLDOWN_HOURS", 6)
        if cooldown_hours <= 0:
            raise ConfigError("COUPLE_COOLDOWN_HOURS must be a positive integer.")

        concurrency = _get_int("BROADCAST_CONCURRENCY", 8)
        if concurrency <= 0:
            raise ConfigError("BROADCAST_CONCURRENCY must be a positive integer.")

        return cls(
            api_id=_get_int("API_ID", required=True),
            api_hash=api_hash,
            bot_token=bot_token,
            mongo_uri=_get("MONGO_URI", required=True),
            mongo_db_name=_get("MONGO_DB_NAME", "opusfun"),
            owner_ids=tuple(owners),
            bot_name=_get("BOT_NAME", "OpusFun"),
            start_img_url=_get("START_IMG_URL"),
            support_url=_get("SUPPORT_URL", "https://t.me/telegram"),
            update_url=_get("UPDATE_URL", "https://t.me/telegram"),
            owner_url=_get("OWNER_URL", "https://t.me/telegram"),
            developer_url=_get("DEVELOPER_URL", "https://t.me/telegram"),
            couple_cooldown_hours=cooldown_hours,
            broadcast_concurrency=concurrency,
            broadcast_sleep=float(_get("BROADCAST_SLEEP", "0.15") or 0.15),
            media_cache_ttl=_get_int("MEDIA_CACHE_TTL", 300),
            log_level=_get("LOG_LEVEL", "INFO").upper(),
            log_file=_get("LOG_FILE", "opusfun.log"),
            workers=_get_int("WORKERS", 8),
            default_language=_get("DEFAULT_LANGUAGE", "en"),
        )


_config: Config | None = None


def get_config() -> Config:
    """Return the process-wide configuration singleton (lazily validated)."""
    global _config
    if _config is None:
        _config = Config.load()
    return _config


def set_config(config: Config) -> None:
    """Inject a configuration object (used by the test-suite)."""
    global _config
    _config = config
