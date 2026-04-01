"""Cog: тикет-система — настройка, панель, закрытие."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..config import Clr
from ..helpers import ensure_admin, get_id, is_admin, is_owner
from ..views.tickets import TicketCreateView, close_ticket

if TYPE_CHECKING:
    from ..core import VoiceSitterBot


class TicketsCog(commands.Cog):
    def __init__(self, bot: VoiceSitterBot) -> None:
        self.bot = bot

    @app_commands.command(name="set_ticket_category", description="Категория для тикетов")
    async def set_category(self, interaction: discord.Interaction,
                           category: discord.CategoryChannel) -> None:
        if not await ensure_admin(interaction):
            return
        self.bot.store.put(interaction.guild_id, "ticket_category_id", str(category.id))
        await interaction.response.send_message(
            f"✅ Ticket category: {category.name}", ephemeral=True,
        )

    @app_commands.command(name="set_ticket_log", description="Канал логов тикетов")
    async def set_log(self, interaction: discord.Interaction,
                      channel: discord.TextChannel) -> None:
        if not await ensure_admin(interaction):
            return
        self.bot.store.put(interaction.guild_id, "ticket_log_channel_id", str(channel.id))
        await interaction.response.send_message(
            f"✅ Ticket log: {channel.mention}", ephemeral=True,
        )

    @app_commands.command(name="set_ticket_support", description="Роль поддержки тикетов")
    async def set_support(self, interaction: discord.Interaction,
                          role: discord.Role) -> None:
        if not await ensure_admin(interaction):
            return
        self.bot.store.put(interaction.guild_id, "ticket_support_role_id", str(role.id))
        await interaction.response.send_message(
            f"✅ Ticket support: {role.mention}", ephemeral=True,
        )

    @app_commands.command(name="ticket_panel", description="Опубликовать панель создания тикета")
    async def panel(self, interaction: discord.Interaction,
                    channel: Optional[discord.TextChannel] = None) -> None:
        if not await ensure_admin(interaction):
            return
        target = channel or interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Нужен текстовый канал.", ephemeral=True)
            return
        embed = discord.Embed(
            title="🎫 Поддержка",
            description="Нажми кнопку ниже, чтобы создать приватный тикет.",
            color=Clr.INFO,
        )
        await target.send(embed=embed, view=TicketCreateView())
        await interaction.response.send_message("✅ Панель тикетов отправлена.", ephemeral=True)

    @app_commands.command(name="ticket_close", description="Закрыть текущий тикет")
    async def close(self, interaction: discord.Interaction,
                    reason: Optional[str] = None) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Только в текстовом тикете.", ephemeral=True)
            return

        topic = interaction.channel.topic or ""
        if not topic.startswith("ticket_owner:"):
            await interaction.response.send_message("Это не тикет-канал.", ephemeral=True)
            return

        owner_id: Optional[int] = None
        try:
            owner_id = int(topic.split(":", 1)[1])
        except (ValueError, IndexError):
            pass

        support_rid = get_id(self.bot, interaction.guild.id, "ticket_support_role_id")
        support_role = interaction.guild.get_role(support_rid) if support_rid else None

        ok = (
            (owner_id is not None and interaction.user.id == owner_id)
            or (isinstance(interaction.user, discord.Member) and is_admin(interaction.user))
            or (isinstance(interaction.user, discord.Member)
                and support_role is not None
                and support_role in interaction.user.roles)
            or is_owner(interaction.user.id)
        )
        if not ok:
            await interaction.response.send_message("Нет прав закрыть этот тикет.", ephemeral=True)
            return

        await interaction.response.send_message("🔒 Закрываю тикет…", ephemeral=True)
        await close_ticket(interaction.channel, interaction.user, reason or "ticket_close")


async def setup(bot: VoiceSitterBot) -> None:
    await bot.add_cog(TicketsCog(bot))
