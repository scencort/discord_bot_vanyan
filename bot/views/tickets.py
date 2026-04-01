"""Views: тикеты — создание и закрытие."""

from __future__ import annotations

import io
import re
from typing import TYPE_CHECKING, Optional

import discord

from ..config import Clr
from ..helpers import get_id, is_owner

if TYPE_CHECKING:
    from ..core import VoiceSitterBot


# ── Утилиты тикетов ─────────────────────────────────────────────────

async def create_ticket(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Тикеты доступны только на сервере.", ephemeral=True)
        return

    guild = interaction.guild
    member = interaction.user
    bot: VoiceSitterBot = interaction.client  # type: ignore[assignment]

    existing = discord.utils.find(
        lambda c: isinstance(c, discord.TextChannel)
        and c.topic and c.topic.startswith(f"ticket_owner:{member.id}"),
        guild.channels,
    )
    if existing is not None:
        await interaction.response.send_message(
            f"У тебя уже есть тикет: {existing.mention}", ephemeral=True,
        )
        return

    cat_id = get_id(bot, guild.id, "ticket_category_id")
    cat = guild.get_channel(cat_id) if cat_id else None
    if cat is not None and not isinstance(cat, discord.CategoryChannel):
        cat = None

    support_rid = get_id(bot, guild.id, "ticket_support_role_id")
    support_role = guild.get_role(support_rid) if support_rid else None

    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
        ),
    }
    me = guild.me
    if me:
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            manage_channels=True, manage_messages=True,
        )
    if support_role:
        overwrites[support_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
        )

    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "-", member.display_name.lower())[:80]
    try:
        ch = await guild.create_text_channel(
            name=f"ticket-{safe_name}", category=cat,
            topic=f"ticket_owner:{member.id}", overwrites=overwrites,
            reason=f"Ticket by {member}",
        )
    except discord.HTTPException:
        await interaction.response.send_message(
            "Не удалось создать тикет. Проверь права бота.", ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="🎫 Тикет создан",
        description=(
            "Опиши свою проблему ниже.\n"
            "Для закрытия используй кнопку или `/ticket_close`."
        ),
        color=Clr.INFO,
    )
    await ch.send(content=member.mention, embed=embed, view=TicketCloseView())
    await interaction.response.send_message(
        f"Тикет создан: {ch.mention}", ephemeral=True,
    )


async def export_transcript(channel: discord.TextChannel) -> discord.File:
    lines: list[str] = []
    async for msg in channel.history(limit=2000, oldest_first=True):
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        author = f"{msg.author} ({msg.author.id})"
        content = msg.content.replace("\n", " ") if msg.content else "<embed/attachment>"
        lines.append(f"[{ts}] {author}: {content}")
    text = "\n".join(lines) if lines else "Пустой тикет"
    return discord.File(io.BytesIO(text.encode()), filename=f"ticket-{channel.id}.txt")


async def close_ticket(channel: discord.TextChannel, closed_by: discord.abc.User,
                       reason: str) -> None:
    bot: VoiceSitterBot = channel.guild._state._get_client()  # type: ignore[attr-defined]
    transcript = await export_transcript(channel)
    log_id = get_id(bot, channel.guild.id, "ticket_log_channel_id")
    log_ch = channel.guild.get_channel(log_id) if log_id else None
    if isinstance(log_ch, (discord.TextChannel, discord.Thread)):
        try:
            await log_ch.send(
                content=f"Тикет **{channel.name}** закрыт · {closed_by.mention} · {reason}",
                file=transcript,
            )
        except discord.HTTPException:
            pass
    try:
        await channel.delete(reason=f"Ticket closed by {closed_by} | {reason}")
    except discord.HTTPException:
        pass


# ── Persistent views ─────────────────────────────────────────────────

class TicketCreateView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Создать тикет", style=discord.ButtonStyle.green,
        emoji="🎫", custom_id="ticket:create",
    )
    async def btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await create_ticket(interaction)


class TicketCloseView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Закрыть тикет", style=discord.ButtonStyle.red,
        emoji="🔒", custom_id="ticket:close",
    )
    async def btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Только в текстовом тикете.", ephemeral=True)
            return

        topic = interaction.channel.topic or ""
        if not topic.startswith("ticket_owner:"):
            await interaction.response.send_message("Это не тикет-канал.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            return

        bot: VoiceSitterBot = interaction.client  # type: ignore[assignment]
        owner_id: Optional[int] = None
        try:
            owner_id = int(topic.split(":", 1)[1])
        except (ValueError, IndexError):
            pass

        support_rid = get_id(bot, guild.id, "ticket_support_role_id")
        support_role = guild.get_role(support_rid) if support_rid else None

        ok = (
            (owner_id is not None and interaction.user.id == owner_id)
            or (isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator)
            or (isinstance(interaction.user, discord.Member) and support_role and support_role in interaction.user.roles)
            or is_owner(interaction.user.id)
        )
        if not ok:
            await interaction.response.send_message("Нет прав.", ephemeral=True)
            return

        await interaction.response.send_message("Закрываю тикет…", ephemeral=True)
        await close_ticket(interaction.channel, interaction.user, "button close")
