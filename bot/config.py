"""Конфигурация бота — переменные окружения, константы, тема."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv(override=True)

# ── Переменные окружения ─────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
DB_PATH: str = os.getenv("DB_PATH", "bot_data.sqlite3").strip()

_VOICE_RAW = os.getenv("VOICE_CHANNEL_ID", "").strip()
_OWNER_RAW = os.getenv("OWNER_USER_ID", "").strip()
_INTENTS_RAW = os.getenv("USE_PRIVILEGED_INTENTS", "1").strip().lower()

# ── Логирование ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("voice-sitter")

# ── Регулярные выражения ─────────────────────────────────────────────
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

# ── Настройки по умолчанию для серверов ──────────────────────────────
DEFAULTS_INT: dict[str, int] = {
    "raid_join_limit": 8,
    "raid_channel_create_limit": 5,
    "raid_role_create_limit": 3,
    "raid_mention_limit": 8,
    "warn_timeout_points": 3,
    "warn_voice_ban_points": 5,
    "warn_ban_points": 7,
    "warn_timeout_minutes": 60,
}

# ── Экономика ────────────────────────────────────────────────────────
DAILY_COOLDOWN_H = 24
DAILY_BASE_REWARD = 100
DAILY_STREAK_BONUS = 20
DAILY_STREAK_CAP = 7

# ── Модерация через чат ──────────────────────────────────────────────
MOD_ACTIONS = frozenset({
    "ban", "kick", "timeout", "voice_ban", "warn",
    "unban", "untimeout", "voice_unban", "unwarn",
})
FLOW_TTL_SEC = 180

# ── Цветовая палитра (нейтрально-тёмная, красный — акцент) ──────────
class Clr:
    PRIMARY  = 0x2B2D42   # тёмный сланец
    SUCCESS  = 0x2D936C   # зелёный
    WARNING  = 0xE9C46A   # тёплый жёлтый
    DANGER   = 0xE63946   # красный акцент
    INFO     = 0x457B9D   # стальной синий
    MOD      = 0x6C5B7B   # приглушённый фиолетовый
    ECONOMY  = 0xF4A261   # тёплый оранжевый
    SOCIAL   = 0xE76F51   # коралловый


# ── Разбор переменных окружения ──────────────────────────────────────
def _parse_int(value: str, name: str) -> int:
    if not value:
        raise ValueError(f"Переменная {name} не задана.")
    try:
        return int(value)
    except ValueError as e:
        raise ValueError(f"{name} должен быть числом.") from e


def _parse_owners(value: str) -> set[int]:
    if not value:
        return set()
    ids: set[int] = set()
    for part in value.split(","):
        s = part.strip()
        if s:
            try:
                ids.add(int(s))
            except ValueError as e:
                raise ValueError("OWNER_USER_ID: ожидаются числа через запятую.") from e
    return ids


VOICE_CHANNEL_ID: int = _parse_int(_VOICE_RAW, "VOICE_CHANNEL_ID")
OWNER_IDS: set[int] = _parse_owners(_OWNER_RAW)
PRIVILEGED_INTENTS: bool = _INTENTS_RAW in {"1", "true", "yes", "on"}


# ── Маленькие утилиты времени (используются повсюду) ─────────────────
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat()
