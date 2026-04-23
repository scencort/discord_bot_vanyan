"""Класс бота, глобальные события, voice-sitter."""

from __future__ import annotations

import asyncio
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .config import (
    BOT_TOKEN, DB_PATH, PRIVILEGED_INTENTS, VOICE_CHANNEL_ID, Clr, log,
)
from .db import Store
from .helpers import safe_reply


class VoiceSitterBot(commands.Bot):
    """Основной бот с Store, persistent views, cog-архитектурой."""

    store: Store
    pending_actions: dict[tuple[int, int], dict]
    _commands_synced: bool
    _connect_lock: asyncio.Lock

    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        intents.messages = True
        intents.message_content = PRIVILEGED_INTENTS
        intents.members = PRIVILEGED_INTENTS

        super().__init__(command_prefix="!", intents=intents)

        self.store = Store(DB_PATH)
        self.pending_actions = {}
        self._commands_synced = False
        self._connect_lock = asyncio.Lock()

    # ── setup_hook — загрузка cog'ов + persistent views ──────────────

    async def setup_hook(self) -> None:
        from .views.admin import AdminPanelView
        from .views.rooms import RoomPanelView
        from .views.tickets import TicketCloseView, TicketCreateView

        self.add_view(AdminPanelView())
        self.add_view(RoomPanelView())
        self.add_view(TicketCreateView())
        self.add_view(TicketCloseView())

        cog_modules = [
            "bot.cogs.setup",
            "bot.cogs.moderation",
            "bot.cogs.rooms",
            "bot.cogs.tickets",
            "bot.cogs.economy",
            "bot.cogs.social",
            "bot.cogs.games",
            "bot.cogs.system",
        ]
        for mod in cog_modules:
            try:
                await self.load_extension(mod)
                log.info("Cog loaded: %s", mod)
            except Exception:
                log.exception("Failed to load cog: %s", mod)

        self.tree.error(self._on_tree_error)

    # ── on_ready ─────────────────────────────────────────────────────

    async def on_ready(self) -> None:
        log.info("Бот запущен как %s (id=%s)", self.user, self.user.id if self.user else "?")

        await self.sync_commands()
        try:
            await self.connect_to_voice()
        except Exception:
            log.exception("Не удалось подключиться к voice в on_ready")

    # ── on_message — диспетчер ───────────────────────────────────────

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        # Admin chat-flow
        from .views.admin import handle_mod_flow
        if await handle_mod_flow(message):
            return

        # Automatic moderation is disabled by design.

    # ── on_voice_state_update — voice-sitter + temp rooms ────────────

    async def on_voice_state_update(
        self, member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if self.user and member.id == self.user.id:
            if before.channel is not None and after.channel is None:
                await asyncio.sleep(3)
                await self.connect_to_voice()
            return
        if member.bot:
            return

        rooms = self.get_cog("RoomsCog")
        if rooms is not None:
            await rooms.maybe_create(member, after)  # type: ignore[attr-defined]
            await rooms.maybe_cleanup(before)  # type: ignore[attr-defined]

    # ── Sync commands ────────────────────────────────────────────────

    async def sync_commands(self) -> None:
        if self._commands_synced:
            return
        try:
            synced = await self.tree.sync()
            self._commands_synced = True
            log.info("Синхронизировано %d slash-команд.", len(synced))
        except discord.HTTPException:
            log.exception("Ошибка синхронизации slash-команд")

    # ── Voice connection ─────────────────────────────────────────────

    async def connect_to_voice(self) -> None:
        async with self._connect_lock:
            try:
                channel = self.get_channel(VOICE_CHANNEL_ID)
                if channel is None:
                    channel = await self.fetch_channel(VOICE_CHANNEL_ID)
            except discord.Forbidden:
                log.error("Нет доступа к voice %s.", VOICE_CHANNEL_ID)
                return
            except discord.NotFound:
                log.error("Voice %s не найден.", VOICE_CHANNEL_ID)
                return
            except discord.HTTPException:
                log.exception("Ошибка при получении voice %s", VOICE_CHANNEL_ID)
                return

            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                log.error("VOICE_CHANNEL_ID — не voice/stage канал.")
                return

            existing = discord.utils.get(self.voice_clients, guild=channel.guild)
            if existing and existing.is_connected():
                if existing.channel and existing.channel.id == channel.id:
                    return
                await existing.move_to(channel)
                return

            try:
                await channel.connect(reconnect=True, self_deaf=False, self_mute=False)
            except discord.ClientException as exc:
                if "Already connected" in str(exc):
                    return
                log.exception("ClientException voice: %s", exc)
            except discord.HTTPException:
                log.exception("Не удалось подключиться к voice")

    # ── Global tree error ────────────────────────────────────────────

    async def _on_tree_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError,
    ) -> None:
        original = error.original if isinstance(error, app_commands.CommandInvokeError) else error
        if isinstance(original, discord.NotFound):
            log.warning("Interaction истёк: %s", original)
            return
        log.exception("Ошибка slash-команды: %s", error)

        text = "Произошла ошибка при выполнении команды."
        if isinstance(error, app_commands.CheckFailure):
            text = "Недостаточно прав."

        await safe_reply(interaction, text, ephemeral=True, ctx="tree.error")

    # ── Cleanup ──────────────────────────────────────────────────────

    async def close(self) -> None:
        self.store.close()
        await super().close()
