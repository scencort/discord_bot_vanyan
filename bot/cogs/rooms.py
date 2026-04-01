"""Cog: временные голосовые комнаты — создание/удаление, slash-команды."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..config import Clr
from ..helpers import ensure_admin, get_id, is_admin

if TYPE_CHECKING:
    from ..core import VoiceSitterBot


class RoomsCog(commands.Cog):
    def __init__(self, bot: VoiceSitterBot) -> None:
        self.bot = bot

    # ── Утилиты ──────────────────────────────────────────────────────

    def _owned_room(self, member: discord.Member) -> Optional[discord.VoiceChannel]:
        """Комната, которой владеет member (или admin видит чужую)."""
        if member.voice is None or not isinstance(member.voice.channel, discord.VoiceChannel):
            return None
        oid = self.bot.store.temp_room_owner(member.voice.channel.id)
        if oid is None:
            return None
        if oid != member.id and not is_admin(member):
            return None
        return member.voice.channel

    # ── Авто-создание при входе в лобби ──────────────────────────────

    async def maybe_create(self, member: discord.Member, after: discord.VoiceState) -> None:
        if after.channel is None or not isinstance(after.channel, discord.VoiceChannel):
            return
        lobby_val = self.bot.store.get(member.guild.id, "temp_voice_lobby_id")
        lobby_id = int(lobby_val) if lobby_val else None
        if lobby_id is None or after.channel.id != lobby_id:
            return

        cat_val = self.bot.store.get(member.guild.id, "temp_voice_category_id")
        cat_id = int(cat_val) if cat_val else None
        cat = member.guild.get_channel(cat_id) if cat_id else after.channel.category
        if cat is not None and not isinstance(cat, discord.CategoryChannel):
            cat = None

        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            member.guild.default_role: discord.PermissionOverwrite(
                view_channel=True, connect=False,
            ),
            member: discord.PermissionOverwrite(
                view_channel=True, connect=True, speak=True, stream=True,
                use_soundboard=True, move_members=True, mute_members=True,
                deafen_members=True, manage_channels=True,
            ),
        }
        me = member.guild.me
        if me:
            overwrites[me] = discord.PermissionOverwrite(
                view_channel=True, connect=True, speak=True, stream=True,
                use_soundboard=True, move_members=True, manage_channels=True,
            )

        try:
            room = await member.guild.create_voice_channel(
                name=f"room-{member.display_name}"[:90],
                category=cat, overwrites=overwrites, reason="Temp room (lobby)",
            )
        except discord.HTTPException:
            return

        self.bot.store.add_temp_room(member.guild.id, room.id, member.id)
        try:
            await member.move_to(room, reason="Temp room")
        except discord.HTTPException:
            pass

    async def maybe_cleanup(self, before: discord.VoiceState) -> None:
        if before.channel is None or not isinstance(before.channel, discord.VoiceChannel):
            return
        oid = self.bot.store.temp_room_owner(before.channel.id)
        if oid is None:
            return
        if before.channel.members:
            return
        self.bot.store.remove_temp_room(before.channel.id)
        try:
            await before.channel.delete(reason="Temp room empty")
        except discord.HTTPException:
            pass

    # ── Slash-команды управления комнатой ─────────────────────────────

    @app_commands.command(name="set_temp_lobby", description="Voice-канал для авто-создания комнат")
    async def set_lobby(self, interaction: discord.Interaction,
                        channel: discord.VoiceChannel) -> None:
        if not await ensure_admin(interaction):
            return
        self.bot.store.put(interaction.guild_id, "temp_voice_lobby_id", str(channel.id))
        await interaction.response.send_message(f"✅ Lobby: {channel.mention}", ephemeral=True)

    @app_commands.command(name="set_temp_category", description="Категория для временных комнат")
    async def set_cat(self, interaction: discord.Interaction,
                      category: discord.CategoryChannel) -> None:
        if not await ensure_admin(interaction):
            return
        self.bot.store.put(interaction.guild_id, "temp_voice_category_id", str(category.id))
        await interaction.response.send_message(f"✅ Категория: {category.name}", ephemeral=True)

    @app_commands.command(name="room_lock", description="Закрыть свою комнату")
    async def room_lock(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            return
        room = self._owned_room(interaction.user)
        if room is None:
            await interaction.response.send_message("Нет твоей комнаты.", ephemeral=True)
            return
        ow = room.overwrites_for(interaction.guild.default_role)
        ow.connect = False
        await room.set_permissions(interaction.guild.default_role, overwrite=ow, reason="room_lock")
        await interaction.response.send_message("🔒 Комната закрыта.", ephemeral=True)

    @app_commands.command(name="room_unlock", description="Открыть свою комнату")
    async def room_unlock(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            return
        room = self._owned_room(interaction.user)
        if room is None:
            await interaction.response.send_message("Нет твоей комнаты.", ephemeral=True)
            return
        ow = room.overwrites_for(interaction.guild.default_role)
        ow.connect = None
        await room.set_permissions(interaction.guild.default_role, overwrite=ow, reason="room_unlock")
        await interaction.response.send_message("🌐 Комната открыта.", ephemeral=True)

    @app_commands.command(name="room_rename", description="Переименовать комнату")
    async def room_rename(self, interaction: discord.Interaction, name: str) -> None:
        if not isinstance(interaction.user, discord.Member):
            return
        room = self._owned_room(interaction.user)
        if room is None:
            await interaction.response.send_message("Нет твоей комнаты.", ephemeral=True)
            return
        await room.edit(name=name[:90], reason="room_rename")
        await interaction.response.send_message("✅ Переименовано.", ephemeral=True)

    @app_commands.command(name="room_limit", description="Лимит пользователей в комнате")
    async def room_limit(self, interaction: discord.Interaction,
                         limit: app_commands.Range[int, 0, 99]) -> None:
        if not isinstance(interaction.user, discord.Member):
            return
        room = self._owned_room(interaction.user)
        if room is None:
            await interaction.response.send_message("Нет твоей комнаты.", ephemeral=True)
            return
        await room.edit(user_limit=limit, reason="room_limit")
        await interaction.response.send_message("✅ Лимит обновлён.", ephemeral=True)


async def setup(bot: VoiceSitterBot) -> None:
    await bot.add_cog(RoomsCog(bot))
