"""Cog: система — планировщик, бэкапы, voice-sitter."""

from __future__ import annotations

import asyncio
import io
import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..config import Clr, VOICE_CHANNEL_ID, log, utcnow
from ..helpers import ensure_admin, get_id, safe_reply

if TYPE_CHECKING:
    from ..core import VoiceSitterBot


# ── Вспомогательные функции ──────────────────────────────────────────

def _parse_utc(value: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


async def build_backup_file(
    bot: VoiceSitterBot,
    guild: discord.Guild,
) -> discord.File:
    payload = {
        "guild_id": guild.id,
        "created_at": utcnow().isoformat(),
        "data": bot.store.backup(guild.id),
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    name = f"backup-{guild.id}-{int(utcnow().timestamp())}.json"
    return discord.File(io.BytesIO(raw.encode()), filename=name)


class SystemCog(commands.Cog):
    def __init__(self, bot: VoiceSitterBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.schedule_runner.start()
        self.keep_connected.start()

    async def cog_unload(self) -> None:
        self.schedule_runner.cancel()
        self.keep_connected.cancel()

    # ══ Schedule commands ════════════════════════════════════════════

    @app_commands.command(name="schedule_reminder", description="Напоминание через N минут")
    async def reminder(self, interaction: discord.Interaction,
                       minutes: app_commands.Range[int, 1, 10080],
                       message: str,
                       channel: Optional[discord.TextChannel] = None) -> None:
        if not await ensure_admin(interaction):
            return
        target = channel or interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Нужен текстовый канал.", ephemeral=True)
            return
        nxt = int((utcnow() + timedelta(minutes=minutes)).timestamp())
        sid = self.bot.store.add_schedule(
            interaction.guild_id, target.id, message, nxt, None, interaction.user.id,
        )
        await interaction.response.send_message(f"✅ Напоминание · ID {sid}", ephemeral=True)

    @app_commands.command(name="schedule_every", description="Каждые N минут")
    async def every(self, interaction: discord.Interaction,
                    minutes: app_commands.Range[int, 1, 10080],
                    message: str,
                    channel: discord.TextChannel) -> None:
        if not await ensure_admin(interaction):
            return
        interval = int(timedelta(minutes=minutes).total_seconds())
        nxt = int((utcnow() + timedelta(minutes=minutes)).timestamp())
        sid = self.bot.store.add_schedule(
            interaction.guild_id, channel.id, message, nxt, interval, interaction.user.id,
        )
        await interaction.response.send_message(f"✅ Повторяющееся · ID {sid}", ephemeral=True)

    @app_commands.command(name="schedule_at", description="Событие на UTC-время (YYYY-MM-DD HH:MM)")
    @app_commands.describe(when_utc="Формат: YYYY-MM-DD HH:MM (UTC)")
    async def at(self, interaction: discord.Interaction,
                 when_utc: str, message: str,
                 channel: discord.TextChannel) -> None:
        if not await ensure_admin(interaction):
            return
        dt = _parse_utc(when_utc)
        if dt is None:
            await interaction.response.send_message("Формат: YYYY-MM-DD HH:MM", ephemeral=True)
            return
        if dt <= utcnow():
            await interaction.response.send_message("Время должно быть в будущем.", ephemeral=True)
            return
        sid = self.bot.store.add_schedule(
            interaction.guild_id, channel.id, message, int(dt.timestamp()), None, interaction.user.id,
        )
        await interaction.response.send_message(f"✅ Событие · ID {sid}", ephemeral=True)

    @app_commands.command(name="schedule_list", description="Список расписаний")
    async def sched_list(self, interaction: discord.Interaction) -> None:
        if not await ensure_admin(interaction):
            return
        rows = self.bot.store.list_schedules(interaction.guild_id)
        if not rows:
            await interaction.response.send_message("Нет активных.", ephemeral=True)
            return
        lines: list[str] = []
        for r in rows:
            ndt = datetime.fromtimestamp(int(r["next_run_at"]), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            iv = r["interval_seconds"]
            iv_text = f"every {int(iv) // 60}m" if iv else "once"
            lines.append(f"#{r['id']} | {iv_text} | {ndt} | {str(r['content'])[:40]}")
        text = "\n".join(lines)
        await interaction.response.send_message(f"```\n{text}\n```", ephemeral=True)

    @app_commands.command(name="schedule_remove", description="Удалить расписание по ID")
    async def sched_remove(self, interaction: discord.Interaction,
                           schedule_id: int) -> None:
        if not await ensure_admin(interaction):
            return
        if self.bot.store.get_schedule(interaction.guild_id, schedule_id) is None:
            await interaction.response.send_message("Не найдено.", ephemeral=True)
            return
        self.bot.store.remove_schedule(interaction.guild_id, schedule_id)
        await interaction.response.send_message("✅ Удалено.", ephemeral=True)

    # ══ Backup commands ══════════════════════════════════════════════

    @app_commands.command(name="backup_create", description="Создать бэкап сервера")
    async def backup_create(self, interaction: discord.Interaction) -> None:
        if not await ensure_admin(interaction):
            return
        if interaction.guild is None:
            return
        f = await build_backup_file(self.bot, interaction.guild)

        bch_id = get_id(self.bot, interaction.guild.id, "backup_channel_id")
        if bch_id:
            ch = interaction.guild.get_channel(bch_id)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                raw = self.bot.store.backup(interaction.guild.id)
                payload = json.dumps(
                    {"guild_id": interaction.guild.id, "created_at": utcnow().isoformat(), "data": raw},
                    ensure_ascii=False, indent=2,
                )
                name = f"backup-{interaction.guild.id}-{int(utcnow().timestamp())}.json"
                bf = discord.File(io.BytesIO(payload.encode()), filename=name)
                try:
                    await ch.send(content=f"Backup by <@{interaction.user.id}>", file=bf)
                except discord.HTTPException:
                    pass

        await interaction.response.send_message("💾 Backup создан.", file=f, ephemeral=True)

    @app_commands.command(name="backup_restore", description="Восстановить из JSON")
    async def backup_restore(self, interaction: discord.Interaction,
                             file: discord.Attachment) -> None:
        if not await ensure_admin(interaction):
            return
        if interaction.guild is None:
            return
        if not file.filename.lower().endswith(".json"):
            await interaction.response.send_message("Нужен JSON.", ephemeral=True)
            return
        raw = await file.read()
        try:
            payload = json.loads(raw.decode())
            data = payload["data"]
        except (ValueError, KeyError):
            await interaction.response.send_message("Некорректный формат.", ephemeral=True)
            return
        self.bot.store.restore(interaction.guild.id, data)
        await interaction.response.send_message(
            "✅ Backup восстановлен. Перезапусти для полной синхронизации.", ephemeral=True,
        )

    # ══ Task loops ═══════════════════════════════════════════════════

    @tasks.loop(seconds=30)
    async def schedule_runner(self) -> None:
        now_ts = int(utcnow().timestamp())
        rows = self.bot.store.due_schedules(now_ts)
        for r in rows:
            guild = self.bot.get_guild(int(r["guild_id"]))
            if guild is None:
                self.bot.store.mark_schedule_ran(int(r["id"]), None)
                continue
            ch = guild.get_channel(int(r["channel_id"]))
            if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                self.bot.store.mark_schedule_ran(int(r["id"]), None)
                continue
            try:
                await ch.send(str(r["content"]))
            except discord.HTTPException:
                pass
            iv = r["interval_seconds"]
            if iv is None:
                self.bot.store.mark_schedule_ran(int(r["id"]), None)
            else:
                nxt = int(r["next_run_at"]) + int(iv)
                while nxt <= now_ts:
                    nxt += int(iv)
                self.bot.store.mark_schedule_ran(int(r["id"]), nxt)

    @schedule_runner.before_loop
    async def _wait_schedule(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=30)
    async def keep_connected(self) -> None:
        try:
            await self.bot.connect_to_voice()
        except Exception:
            log.exception("Voice reconnect failed")

    @keep_connected.before_loop
    async def _wait_voice(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: VoiceSitterBot) -> None:
    await bot.add_cog(SystemCog(bot))
