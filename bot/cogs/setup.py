"""Cog: первоначальная настройка — setup_owner_hub, bind каналов, server_init, публикация панелей."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..config import Clr, OWNER_IDS, VOICE_CHANNEL_ID
from ..helpers import ensure_admin, get_id, is_owner, safe_defer, safe_reply
from ..views.admin import AdminPanelView
from ..views.rooms import RoomPanelView
from ..views.tickets import TicketCreateView

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

    # ═══════════════════════════════════════════════════════════════════
    #  /server_init — полная инициализация сервера «Для друзей»
    # ═══════════════════════════════════════════════════════════════════

    @app_commands.command(
        name="server_init",
        description="Полная настройка сервера: роли, каналы, права, битрейт",
    )
    @app_commands.describe(
        server_name="Название сервера (по умолчанию «Для друзей»)",
    )
    async def server_init(
        self, interaction: discord.Interaction,
        server_name: str = "Для друзей",
    ) -> None:
        if not await ensure_admin(interaction):
            return
        guild = interaction.guild
        if guild is None:
            return
        if not await safe_defer(interaction, ctx="server_init"):
            return

        owners = resolve_owners(guild)
        s = self.bot.store
        me = guild.me
        log: list[str] = []

        # ── Переименовать сервер ─────────────────────────────────────
        try:
            await guild.edit(name=server_name[:100], reason="server_init")
            log.append(f"✏️ Сервер: **{server_name}**")
        except discord.HTTPException:
            log.append("⚠ Не удалось переименовать сервер")

        # ── Роли ─────────────────────────────────────────────────────
        role_defs: list[tuple[str, discord.Colour, discord.Permissions, bool]] = [
            # (name, color, permissions, hoist)
            ("👑 Владелец", discord.Colour(0xFFD700), discord.Permissions.all(), True),
            ("🛡 Админ", discord.Colour(0xE63946), discord.Permissions(
                administrator=True,
            ), True),
            ("⚔ Модератор", discord.Colour(0x457B9D), discord.Permissions(
                view_channel=True, send_messages=True, read_message_history=True,
                manage_messages=True, kick_members=True, ban_members=True,
                moderate_members=True, mute_members=True, deafen_members=True,
                move_members=True, manage_nicknames=True, view_audit_log=True,
                manage_channels=True, manage_roles=True,
            ), True),
            ("🎮 Участник", discord.Colour(0x2D936C), discord.Permissions(
                view_channel=True, send_messages=True, read_message_history=True,
                connect=True, speak=True, stream=True, use_soundboard=True,
                use_voice_activation=True, add_reactions=True,
                attach_files=True, embed_links=True, use_external_emojis=True,
                use_application_commands=True,
            ), True),
            ("🔇 Мут", discord.Colour(0x6C757D), discord.Permissions(
                view_channel=True, read_message_history=True,
                connect=True,
            ), False),
        ]

        roles: dict[str, discord.Role] = {}
        bot_top = me.top_role if me else None

        for rname, rcolor, rperms, rhoist in role_defs:
            existing = discord.utils.get(guild.roles, name=rname)
            if existing:
                roles[rname] = existing
                log.append(f"🔄 Роль уже есть: {rname}")
            else:
                try:
                    r = await guild.create_role(
                        name=rname, colour=rcolor, permissions=rperms,
                        hoist=rhoist, mentionable=False, reason="server_init",
                    )
                    roles[rname] = r
                    log.append(f"✅ Роль: {r.mention}")
                except discord.HTTPException:
                    log.append(f"⚠ Не удалось создать роль {rname}")

        # Выдать 👑 Владелец owner'ам
        owner_role = roles.get("👑 Владелец")
        if owner_role:
            for om in owners:
                try:
                    await om.add_roles(owner_role, reason="server_init: owner")
                except discord.HTTPException:
                    pass
            if owners:
                log.append(f"👑 Роль владельца выдана: {', '.join(m.mention for m in owners)}")

        # Права @everyone: только базовый просмотр
        try:
            await guild.default_role.edit(permissions=discord.Permissions(
                view_channel=True, read_message_history=True,
                connect=True, speak=True, use_voice_activation=True,
                use_application_commands=True, add_reactions=True,
            ), reason="server_init: default role")
            log.append("⚙ @everyone настроен")
        except discord.HTTPException:
            pass

        # Хелпер: perm overwrites
        admin_role = roles.get("🛡 Админ")
        mod_role = roles.get("⚔ Модератор")
        member_role = roles.get("🎮 Участник")
        mute_role = roles.get("🔇 Мут")

        def _base_ow() -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
            ow: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
            if me:
                ow[me] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    manage_channels=True, manage_messages=True, manage_roles=True,
                    connect=True, speak=True, move_members=True,
                )
            return ow

        def _staff_ow() -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
            """Каналы скрыты от всех, видны только staff + bot."""
            ow = _base_ow()
            ow[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
            if owner_role:
                ow[owner_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    manage_messages=True,
                )
            if admin_role:
                ow[admin_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    manage_messages=True,
                )
            if mod_role:
                ow[mod_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                )
            return ow

        def _readonly_ow() -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
            """Все видят, но писать нельзя."""
            ow = _base_ow()
            ow[guild.default_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=False, read_message_history=True,
                add_reactions=True,
            )
            if mute_role:
                ow[mute_role] = discord.PermissionOverwrite(send_messages=False, add_reactions=False)
            return ow

        def _public_ow() -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
            ow = _base_ow()
            ow[guild.default_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
            )
            if mute_role:
                ow[mute_role] = discord.PermissionOverwrite(send_messages=False, add_reactions=False)
            return ow

        # ── Категория: 📋 Информация ────────────────────────────────
        cat_info = await guild.create_category("📋 Информация", overwrites=_readonly_ow(), reason="server_init")
        ch_rules = await guild.create_text_channel("📜・правила", category=cat_info, overwrites=_readonly_ow(), reason="server_init")
        ch_announce = await guild.create_text_channel("📢・объявления", category=cat_info, overwrites=_readonly_ow(), reason="server_init")
        ch_welcome = await guild.create_text_channel("👋・приветствие", category=cat_info, overwrites=_readonly_ow(), reason="server_init")
        log.append(f"📋 Категория: **Информация** ({ch_rules.mention}, {ch_announce.mention}, {ch_welcome.mention})")

        # Заполнить правила
        rules_embed = discord.Embed(
            title="📜 Правила сервера",
            description=(
                "**1.** Уважайте друг друга\n"
                "**2.** Без спама и флуда\n"
                "**3.** Запрещён NSFW контент\n"
                "**4.** Не злоупотребляйте упоминаниями\n"
                "**5.** Слушайте модераторов\n"
                "**6.** Никакой рекламы без разрешения\n"
                "**7.** Используйте каналы по назначению\n\n"
                "Нарушение правил → warn → timeout → ban"
            ),
            color=Clr.PRIMARY,
        )
        await ch_rules.send(embed=rules_embed)

        # ── Категория: 💬 Общение ───────────────────────────────────
        cat_chat = await guild.create_category("💬 Общение", overwrites=_public_ow(), reason="server_init")
        ch_main = await guild.create_text_channel("💬・основной", category=cat_chat, overwrites=_public_ow(), topic="Общий чат для всех", reason="server_init")
        ch_games = await guild.create_text_channel("🎮・игровой", category=cat_chat, overwrites=_public_ow(), topic="Обсуждение игр", reason="server_init")
        ch_memes = await guild.create_text_channel("🖼・мемы", category=cat_chat, overwrites=_public_ow(), topic="Мемы и картинки", reason="server_init")
        ch_music = await guild.create_text_channel("🎵・музыка", category=cat_chat, overwrites=_public_ow(), topic="Делитесь музыкой", reason="server_init")
        ch_cmd = await guild.create_text_channel("🤖・команды-бота", category=cat_chat, overwrites=_public_ow(), topic="Команды бота", reason="server_init")
        log.append(f"💬 Категория: **Общение** (5 каналов)")

        # ── Категория: 🔊 Голосовые ─────────────────────────────────
        cat_voice = await guild.create_category("🔊 Голосовые", overwrites=_base_ow(), reason="server_init")
        vc_general = await guild.create_voice_channel("🔊 Общий", category=cat_voice, bitrate=96000, user_limit=0, reason="server_init")
        vc_game1 = await guild.create_voice_channel("🎮 Игровая 1", category=cat_voice, bitrate=96000, user_limit=5, reason="server_init")
        vc_game2 = await guild.create_voice_channel("🎮 Игровая 2", category=cat_voice, bitrate=96000, user_limit=5, reason="server_init")
        vc_chill = await guild.create_voice_channel("☕ Чиллзона", category=cat_voice, bitrate=64000, user_limit=10, reason="server_init")
        vc_music = await guild.create_voice_channel("🎵 Музыка", category=cat_voice, bitrate=128000, user_limit=0, reason="server_init")
        vc_afk = await guild.create_voice_channel("💤 AFK", category=cat_voice, bitrate=8000, user_limit=0, reason="server_init")
        log.append(f"🔊 Категория: **Голосовые** (6 каналов, bitrate 64–128k)")

        # AFK-канал сервера
        try:
            await guild.edit(afk_channel=vc_afk, afk_timeout=600, reason="server_init")
            log.append("💤 AFK-канал настроен (10 мин)")
        except discord.HTTPException:
            pass

        # ── Категория: 🏠 Временные комнаты ──────────────────────────
        cat_temp = await guild.create_category("🏠 Временные комнаты", overwrites=_base_ow(), reason="server_init")
        vc_lobby = await guild.create_voice_channel(
            "📞 Создать комнату", category=cat_temp, bitrate=96000,
            user_limit=1, reason="server_init",
        )
        ch_room_panel = await guild.create_text_channel(
            "🏠・управление", category=cat_temp, overwrites=_public_ow(),
            topic="Создание и управление временными комнатами", reason="server_init",
        )
        s.put(guild.id, "temp_voice_lobby_id", str(vc_lobby.id))
        s.put(guild.id, "temp_voice_category_id", str(cat_temp.id))
        s.put(guild.id, "temp_create_text_channel_id", str(ch_room_panel.id))
        log.append(f"🏠 **Временные комнаты**: лобби {vc_lobby.mention} + панель {ch_room_panel.mention}")

        # Панель комнат
        room_embed = discord.Embed(
            title="🏠 Голосовые комнаты",
            description=(
                "Создавайте и управляйте личными голосовыми комнатами.\n\n"
                "📋 **Как это работает:**\n"
                "• Нажмите **Создать** → выберите тип → заполните параметры\n"
                "• Используйте меню **Управление** для настройки\n"
                "• Зайдите в «📞 Создать комнату» — комната создастся автоматически\n"
                "• Комната удаляется, когда все выйдут"
            ),
            color=Clr.SUCCESS,
        )
        await ch_room_panel.send(embed=room_embed, view=RoomPanelView())

        # ── Категория: 🎫 Поддержка ─────────────────────────────────
        cat_support = await guild.create_category("🎫 Поддержка", overwrites=_public_ow(), reason="server_init")
        ch_ticket = await guild.create_text_channel("🎫・создать-тикет", category=cat_support, overwrites=_public_ow(), reason="server_init")
        s.put(guild.id, "ticket_category_id", str(cat_support.id))

        # Панель тикетов
        ticket_embed = discord.Embed(
            title="🎫 Поддержка",
            description="Нажми кнопку ниже, чтобы создать приватный тикет.",
            color=Clr.INFO,
        )
        await ch_ticket.send(embed=ticket_embed, view=TicketCreateView())
        log.append(f"🎫 **Поддержка**: {ch_ticket.mention}")

        # ── Категория: 🛡 Администрация (скрытая) ────────────────────
        cat_admin = await guild.create_category("🛡 Администрация", overwrites=_staff_ow(), reason="server_init")
        ch_modlog = await guild.create_text_channel("📝・логи-модерации", category=cat_admin, overwrites=_staff_ow(), reason="server_init")
        ch_alerts = await guild.create_text_channel("🚨・алерты", category=cat_admin, overwrites=_staff_ow(), reason="server_init")
        ch_backup = await guild.create_text_channel("💾・бэкапы", category=cat_admin, overwrites=_staff_ow(), reason="server_init")

        # Owner-only admin panel
        owner_ow = _staff_ow()
        if mod_role and mod_role in owner_ow:
            del owner_ow[mod_role]  # только owner + admin
        ch_admin_panel = await guild.create_text_channel("🎛・центр-управления", category=cat_admin, overwrites=owner_ow, reason="server_init")

        s.put(guild.id, "modlog_channel_id", str(ch_modlog.id))
        s.put(guild.id, "alert_channel_id", str(ch_alerts.id))
        s.put(guild.id, "backup_channel_id", str(ch_backup.id))
        s.put(guild.id, "owner_admin_channel_id", str(ch_admin_panel.id))
        log.append(f"🛡 **Администрация**: логи, алерты, бэкапы, центр управления")

        # Admin panel embed
        owners_text = ", ".join(f"<@{oid}>" for oid in sorted(OWNER_IDS)) or "не заданы"
        admin_embed = discord.Embed(
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
        admin_embed.set_footer(text="Owner-only panel")
        await ch_admin_panel.send(embed=admin_embed, view=AdminPanelView())

        # Если есть ticket support role — назначить модераторов
        if mod_role:
            s.put(guild.id, "ticket_support_role_id", str(mod_role.id))
            log.append(f"🎫 Роль поддержки тикетов: {mod_role.mention}")

        # ── Итог ─────────────────────────────────────────────────────
        summary = "\n".join(log)
        embed = discord.Embed(
            title="🚀 Сервер настроен!",
            description=summary,
            color=Clr.SUCCESS,
        )
        embed.set_footer(text="Команды: /profile, /balance, /timely, /shop, /duel, /rps, /slots, /marry, /myrole, /report")
        await safe_reply(interaction, "", embed=embed, ctx="server_init:done")

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
