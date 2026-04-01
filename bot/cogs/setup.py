"""Cog: первоначальная настройка — setup_owner_hub, bind каналов, публикация панелей."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..config import Clr, OWNER_IDS
from ..helpers import ensure_admin, get_id, is_owner, safe_defer, safe_reply
from ..views.admin import AdminPanelView
from ..views.rooms import RoomPanelView

if TYPE_CHECKING:
    from ..core import VoiceSitterBot


def resolve_owners(guild: discord.Guild) -> list[discord.Member]:
    return [m for oid in sorted(OWNER_IDS) if (m := guild.get_member(oid)) is not None]


async def publish_panels(
    bot: VoiceSitterBot, guild: discord.Guild,
) -> tuple[Optional[discord.TextChannel], Optional[discord.TextChannel]]:
    """Публикация embed-панелей в назначенные каналы."""
    temp_id = get_id(bot, guild.id, "temp_create_text_channel_id")
    admin_id = get_id(bot, guild.id, "owner_admin_channel_id")
    temp_ch = guild.get_channel(temp_id) if temp_id else None
    admin_ch = guild.get_channel(admin_id) if admin_id else None

    result_temp: Optional[discord.TextChannel] = None
    result_admin: Optional[discord.TextChannel] = None

    if isinstance(temp_ch, discord.TextChannel):
        embed = discord.Embed(
            title="🏠 Голосовые комнаты",
            description=(
                "Создавайте и управляйте личными голосовыми комнатами.\n\n"
                "📋 **Как это работает:**\n"
                "• Нажмите **Создать** → выберите тип → заполните параметры\n"
                "• Используйте меню **Управление** для настройки\n"
                "• Комната удаляется автоматически, когда все выйдут"
            ),
            color=Clr.SUCCESS,
        )
        await temp_ch.send(embed=embed, view=RoomPanelView())
        result_temp = temp_ch

    if isinstance(admin_ch, discord.TextChannel):
        owners_text = ", ".join(f"<@{oid}>" for oid in sorted(OWNER_IDS)) or "не заданы"
        embed = discord.Embed(
            title="🎛 Центр управления",
            description=(
                f"**Owners:** {owners_text}\n\n"
                "🛡 **Модерация** — ban, kick, timeout, warn и снятие\n"
                "⚙ **Настройки** — каналы логов, алертов, backup\n"
                "⏰ **Расписание** — таймеры и напоминания\n\n"
                "Выберите раздел ниже или используйте кнопки быстрого доступа."
            ),
            color=Clr.PRIMARY,
        )
        embed.set_footer(text="Owner-only panel")
        await admin_ch.send(embed=embed, view=AdminPanelView())
        result_admin = admin_ch

    return result_temp, result_admin


class SetupCog(commands.Cog):
    def __init__(self, bot: VoiceSitterBot) -> None:
        self.bot = bot

    # ── /setup_owner_hub ─────────────────────────────────────────────

    @app_commands.command(
        name="setup_owner_hub",
        description="Создать каналы: публичный temp-room и приватный owner-admin",
    )
    @app_commands.describe(
        temp_channel_name="Название канала создания комнат",
        admin_channel_name="Название owner-канала",
        category_name="Категория для каналов",
    )
    async def setup_owner_hub(
        self, interaction: discord.Interaction,
        temp_channel_name: str = "создать-комнату",
        admin_channel_name: str = "owner-admin",
        category_name: str = "bot-hub",
    ) -> None:
        if not await ensure_admin(interaction):
            return
        guild = interaction.guild
        if guild is None:
            await safe_reply(interaction, "Только на сервере.", ctx="setup")
            return
        if not await safe_defer(interaction, ctx="setup_owner_hub"):
            return

        if not OWNER_IDS:
            await safe_reply(interaction, "OWNER_USER_ID не задан в .env.", ctx="setup:no_ids")
            return
        owners = resolve_owners(guild)
        if not owners:
            await safe_reply(interaction, "Owner ID из .env не найдены на сервере.", ctx="setup:no_owners")
            return

        s = self.bot.store

        # Категория
        cat_id = get_id(self.bot, guild.id, "owner_hub_category_id")
        cat = guild.get_channel(cat_id) if cat_id else None
        if not isinstance(cat, discord.CategoryChannel):
            cat = await guild.create_category(name=category_name[:90], reason="Setup owner hub")
            s.put(guild.id, "owner_hub_category_id", str(cat.id))

        # Публичный канал (temp room)
        temp_id = get_id(self.bot, guild.id, "temp_create_text_channel_id")
        temp_ch = guild.get_channel(temp_id) if temp_id else None
        if not isinstance(temp_ch, discord.TextChannel):
            pub_ow: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                ),
            }
            me = guild.me
            if me:
                pub_ow[me] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    manage_channels=True, manage_messages=True,
                )
            temp_ch = await guild.create_text_channel(
                name=temp_channel_name[:90], category=cat,
                overwrites=pub_ow, reason="Setup: temp create channel",
            )
            s.put(guild.id, "temp_create_text_channel_id", str(temp_ch.id))

        # Приватный owner-канал
        adm_id = get_id(self.bot, guild.id, "owner_admin_channel_id")
        adm_ch = guild.get_channel(adm_id) if adm_id else None
        if not isinstance(adm_ch, discord.TextChannel):
            priv_ow: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
            }
            me = guild.me
            if me:
                priv_ow[me] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    manage_channels=True, manage_messages=True,
                )
            for om in owners:
                priv_ow[om] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                )
            adm_ch = await guild.create_text_channel(
                name=admin_channel_name[:90], category=cat,
                overwrites=priv_ow, reason="Setup: owner admin channel",
            )
            s.put(guild.id, "owner_admin_channel_id", str(adm_ch.id))

        pt, pa = await publish_panels(self.bot, guild)
        t_text = pt.mention if pt else "(пропущено)"
        a_text = pa.mention if pa else "(пропущено)"
        await safe_reply(
            interaction,
            f"✅ Готово!\n"
            f"🏠 Публичный канал: {t_text}\n"
            f"🎛 Owner-канал: {a_text}",
            ctx="setup:done",
        )

    # ── Привязка существующих каналов ────────────────────────────────

    @app_commands.command(
        name="bind_temp_channel",
        description="Привязать канал как public temp-create",
    )
    async def bind_temp(self, interaction: discord.Interaction,
                        channel: discord.TextChannel) -> None:
        if not await ensure_admin(interaction):
            return
        if interaction.guild is None:
            return
        self.bot.store.put(interaction.guild.id, "temp_create_text_channel_id", str(channel.id))
        embed = discord.Embed(
            title="🏠 Голосовые комнаты",
            description=(
                "Создавайте и управляйте личными голосовыми комнатами.\n\n"
                "📋 **Как это работает:**\n"
                "• Нажмите **Создать** → выберите тип → заполните параметры\n"
                "• Используйте меню **Управление** для настройки\n"
                "• Комната удаляется автоматически, когда все выйдут"
            ),
            color=Clr.SUCCESS,
        )
        await channel.send(embed=embed, view=RoomPanelView())
        await interaction.response.send_message(
            f"✅ Канал привязан: {channel.mention}", ephemeral=True,
        )

    @app_commands.command(
        name="bind_admin_channel",
        description="Привязать канал как private owner-admin",
    )
    async def bind_admin(self, interaction: discord.Interaction,
                         channel: discord.TextChannel) -> None:
        if not await ensure_admin(interaction):
            return
        guild = interaction.guild
        if guild is None or not OWNER_IDS:
            await interaction.response.send_message("OWNER_USER_ID пуст.", ephemeral=True)
            return
        owners = resolve_owners(guild)
        if not owners:
            await interaction.response.send_message("Owner не найдены.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await channel.set_permissions(
            guild.default_role, overwrite=discord.PermissionOverwrite(view_channel=False),
        )
        for om in owners:
            await channel.set_permissions(
                om,
                overwrite=discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                ),
            )
        me = guild.me
        if me:
            await channel.set_permissions(
                me,
                overwrite=discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    manage_channels=True, manage_messages=True,
                ),
            )

        self.bot.store.put(guild.id, "owner_admin_channel_id", str(channel.id))
        await publish_panels(self.bot, guild)
        await interaction.followup.send(
            f"✅ Owner-канал: {channel.mention}", ephemeral=True,
        )


async def setup(bot: VoiceSitterBot) -> None:
    await bot.add_cog(SetupCog(bot))
