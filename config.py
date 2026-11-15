"""Загрузка настроек из .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ENV_PATH = BASE_DIR / ".env"
DB_PATH = DATA_DIR / "tgleadmonitor.db"
# Telethon сам добавит расширение .session -> data/userbot.session
SESSION_PATH = DATA_DIR / "userbot"


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_id: int
    api_id: int
    api_hash: str


def load_config() -> Config | None:
    """Возвращает конфиг или None, если .env отсутствует / заполнен не полностью."""
    load_dotenv(ENV_PATH, override=True)
    token = os.getenv("BOT_TOKEN", "").strip()
    api_hash = os.getenv("API_HASH", "").strip()
    try:
        admin_id = int(os.getenv("ADMIN_ID", "").strip())
        api_id = int(os.getenv("API_ID", "").strip())
    except ValueError:
        return None
    if not token or not api_hash or not admin_id or not api_id:
        return None
    return Config(bot_token=token, admin_id=admin_id, api_id=api_id, api_hash=api_hash)
