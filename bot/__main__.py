"""Точка входа: python -m bot"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import aiohttp
import discord

from .config import BOT_TOKEN, DB_PATH, log
from .core import VoiceSitterBot


def validate_env() -> None:
    if not BOT_TOKEN:
        raise ValueError("Переменная BOT_TOKEN не задана в .env")


def ensure_data_dir() -> None:
    p = Path(DB_PATH)
    if p.parent and str(p.parent) not in ("", "."):
        p.parent.mkdir(parents=True, exist_ok=True)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (
        aiohttp.ClientError, asyncio.TimeoutError,
        ConnectionResetError, OSError, discord.GatewayNotFound,
    )):
        return True
    if isinstance(exc, AttributeError) and "'NoneType' object has no attribute 'sequence'" in str(exc):
        return True
    text = str(exc).lower()
    markers = (
        "cannot connect to host gateway.discord.gg",
        "cannot connect to host discord.com",
        "clientconnectorerror",
        "connection reset",
        "gateway",
    )
    return any(m in text for m in markers)


def main() -> None:
    validate_env()
    ensure_data_dir()
    delay = 1

    while True:
        bot = VoiceSitterBot()
        try:
            bot.run(BOT_TOKEN, log_handler=None)
            log.warning("Client остановлен без исключения.")
            return
        except KeyboardInterrupt:
            log.info("Остановка по Ctrl+C")
            return
        except discord.LoginFailure:
            log.error("Неверный BOT_TOKEN.")
            raise
        except discord.PrivilegedIntentsRequired:
            log.error(
                "Privileged intents не включены в Developer Portal. "
                "Включи SERVER MEMBERS и MESSAGE CONTENT, "
                "или добавь USE_PRIVILEGED_INTENTS=0 в .env.",
            )
            raise
        except Exception as exc:
            if not _is_retryable(exc):
                log.exception("Фатальная ошибка")
                raise
            log.warning(
                "Сбой gateway (%s). Повтор через %dс.",
                exc.__class__.__name__, delay,
            )
            time.sleep(delay)
            delay = min(delay * 2, 60)


if __name__ == "__main__":
    main()
