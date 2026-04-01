"""Views: панель временных голосовых комнат — пошаговое создание и управление."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord

from ..config import Clr
from ..helpers import resolve_members, safe_modal

if TYPE_CHECKING:
    from ..core import VoiceSitterBot


# ── Утилиты комнат ───────────────────────────────────────────────────

def find_room(guild: discord.Guild, owner_id: int) -> Optional[discord.VoiceChannel]:
    from ..core import VoiceSitterBot
    bot: VoiceSitterBot = guild._state._get_client()  # type: ignore[attr-defined]
    for vc in guild.voice_channels:
        if bot.store.temp_room_owner(vc.id) == owner_id:
            return vc
    return None


def is_private(room: discord.VoiceChannel) -> bool:
    return room.overwrites_for(room.guild.default_role).connect is False


async def create_room(
    member: discord.Member, *, name: Optional[str] = None,
    limit: int = 0, private: bool = False,
    allowed: Optional[list[discord.Member]] = None,
) -> tuple[discord.VoiceChannel, bool]:
    from ..core import VoiceSitterBot
    bot: VoiceSitterBot = member.guild._state._get_client()  # type: ignore[attr-defined]
    existing = find_room(member.guild, member.id)
    if existing:
        return existing, False

    cat_id_val = bot.store.get(member.guild.id, "temp_voice_category_id")
    cat_id = int(cat_id_val) if cat_id_val else None
    cat = member.guild.get_channel(cat_id) if cat_id else None

    if cat is None:
        lobby_val = bot.store.get(member.guild.id, "temp_voice_lobby_id")
        lobby_id = int(lobby_val) if lobby_val else None
        lobby = member.guild.get_channel(lobby_id) if lobby_id else None
        if isinstance(lobby, discord.VoiceChannel):
            cat = lobby.category

    if cat is not None and not isinstance(cat, discord.CategoryChannel):
        cat = None

    default_ow = discord.PermissionOverwrite(
        view_channel=True, connect=not private,
    )
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        member.guild.default_role: default_ow,
        member: discord.PermissionOverwrite(
            view_channel=True, connect=True, speak=True, stream=True,
            use_soundboard=True, move_members=True, mute_members=True,
            deafen_members=True, manage_channels=True,
        ),
    }
    for a in allowed or []:
        if a.id != member.id:
            overwrites[a] = discord.PermissionOverwrite(
                view_channel=True, connect=True, speak=True,
                stream=True, use_soundboard=True,
            )
    me = member.guild.me
    if me:
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True, connect=True, speak=True, stream=True,
            use_soundboard=True, move_members=True, manage_channels=True,
        )

    room = await member.guild.create_voice_channel(
        name=(name or f"room-{member.display_name}")[:90],
        category=cat, overwrites=overwrites, user_limit=limit,
        reason="Temp room",
    )
    bot.store.add_temp_room(member.guild.id, room.id, member.id)
    return room, True


# ── Модалка создания комнаты (шаг 2 визарда) ────────────────────────

class RoomCreateModal(discord.ui.Modal, title="🔊 Новая голосовая комната"):
    def __init__(self, room_private: bool) -> None:
        super().__init__()
        self._private = room_private

    room_name = discord.ui.TextInput(
        label="Название",
        required=False, max_length=90,
        placeholder="Мой канал (по умолчанию — твой ник)",
    )
    room_limit = discord.ui.TextInput(
        label="Лимит участников (0 = без лимита)",
        required=False, default="0", max_length=2,
    )
    allowed_users = discord.ui.TextInput(
        label="Доступ для (ID/@/ник через запятую)",
        required=False, max_length=400,
        placeholder="Только для закрытой комнаты",
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return

        try:
            lim = int(self.room_limit.value.strip() or "0")
        except ValueError:
            await interaction.response.send_message("Лимит — число 0–99.", ephemeral=True)
            return
        if not 0 <= lim <= 99:
            await interaction.response.send_message("Лимит — число 0–99.", ephemeral=True)
            return

        allowed: list[discord.Member] = []
        missed: list[str] = []
        if self._private and (self.allowed_users.value or "").strip():
            allowed, missed = await resolve_members(
                interaction.guild, self.allowed_users.value,
            )

        try:
            room, is_new = await create_room(
                interaction.user,
                name=(self.room_name.value or "").strip() or None,
                limit=lim, private=self._private, allowed=allowed,
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                "Не удалось создать комнату. Проверь права бота.", ephemeral=True,
            )
            return

        if interaction.user.voice:
            try:
                await interaction.user.move_to(room, reason="Temp room")
            except discord.HTTPException:
                pass

        status = "✅ Создана" if is_new else "ℹ️ Уже существует"
        mode = "🔒 Закрытая" if self._private else "🌐 Открытая"
        missed_note = f"\n⚠ Не найдены: {', '.join(missed[:5])}" if missed else ""

        embed = discord.Embed(
            title=f"{status}: {room.name}",
            description=(
                f"Канал: {room.mention}\n"
                f"Режим: {mode} · Лимит: {room.user_limit or '∞'}"
                f"{missed_note}"
            ),
            color=Clr.SUCCESS,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Модалка управления доступом ──────────────────────────────────────

class RoomAccessModal(discord.ui.Modal):
    def __init__(self, add: bool) -> None:
        self._add = add
        title = "➕ Добавить доступ" if add else "➖ Убрать доступ"
        super().__init__(title=title)
        self.users_field = discord.ui.TextInput(
            label="Пользователи (ID/@/ник через запятую)",
            required=True, max_length=400,
        )
        self.add_item(self.users_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return

        room = find_room(interaction.guild, interaction.user.id)
        if room is None:
            await interaction.response.send_message(
                "У тебя нет временной комнаты.", ephemeral=True,
            )
            return
        if not is_private(room) and self._add:
            await interaction.response.send_message(
                "Комната открыта — доступ не нужен. Сначала закрой её.", ephemeral=True,
            )
            return

        members, missed = await resolve_members(
            interaction.guild, self.users_field.value,
        )
        if not members:
            await interaction.response.send_message(
                "Никого не удалось найти.", ephemeral=True,
            )
            return

        changed = 0
        for m in members:
            ow = room.overwrites_for(m)
            if self._add:
                ow.connect = True
                ow.view_channel = True
            else:
                ow.connect = None
                ow.view_channel = None
            try:
                if ow.is_empty():
                    await room.set_permissions(m, overwrite=None, reason="Room access")
                else:
                    await room.set_permissions(m, overwrite=ow, reason="Room access")
                changed += 1
            except discord.HTTPException:
                pass

        action = "добавлен" if self._add else "удалён"
        missed_note = f"\n⚠ Не найдены: {', '.join(missed[:5])}" if missed else ""
        await interaction.response.send_message(
            f"Доступ {action} для {changed} пользователей.{missed_note}",
            ephemeral=True,
        )


# ── Выбор типа комнаты (шаг 1 визарда) ──────────────────────────────

class RoomTypeView(discord.ui.View):
    """Эфемерное меню выбора типа комнаты перед открытием модалки."""

    def __init__(self) -> None:
        super().__init__(timeout=60)

    @discord.ui.button(label="🌐 Открытая", style=discord.ButtonStyle.success)
    async def open_room(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_modal(interaction, RoomCreateModal(room_private=False), ctx="room:open")

    @discord.ui.button(label="🔒 Закрытая", style=discord.ButtonStyle.secondary)
    async def private_room(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_modal(interaction, RoomCreateModal(room_private=True), ctx="room:private")

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Создание отменено.", view=None, embed=None)


# ── Управление комнатой (select) ─────────────────────────────────────

class RoomManageSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label="Открыть комнату", value="open",
                                 emoji="🌐", description="Разрешить вход всем"),
            discord.SelectOption(label="Закрыть комнату", value="close",
                                 emoji="🔒", description="Ограничить вход"),
            discord.SelectOption(label="Добавить доступ", value="add",
                                 emoji="➕", description="Пустить пользователя"),
            discord.SelectOption(label="Убрать доступ", value="remove",
                                 emoji="➖", description="Убрать пользователя"),
        ]
        super().__init__(
            placeholder="⚙ Управление комнатой…",
            options=options, custom_id="room:manage",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return

        room = find_room(interaction.guild, interaction.user.id)
        if room is None:
            await interaction.response.send_message(
                "У тебя нет временной комнаты. Сначала создай.", ephemeral=True,
            )
            return

        action = self.values[0]

        if action == "open":
            ow = room.overwrites_for(interaction.guild.default_role)
            ow.connect = True
            await room.set_permissions(
                interaction.guild.default_role, overwrite=ow, reason="Room open",
            )
            await interaction.response.send_message(
                "🌐 Комната открыта для всех.", ephemeral=True,
            )

        elif action == "close":
            ow = room.overwrites_for(interaction.guild.default_role)
            ow.connect = False
            await room.set_permissions(
                interaction.guild.default_role, overwrite=ow, reason="Room private",
            )
            await interaction.response.send_message(
                "🔒 Комната переведена в закрытый режим.", ephemeral=True,
            )

        elif action == "add":
            await safe_modal(
                interaction, RoomAccessModal(add=True), ctx="room:add_access",
            )

        elif action == "remove":
            await safe_modal(
                interaction, RoomAccessModal(add=False), ctx="room:remove_access",
            )


# ── Постоянная панель в текстовом канале ─────────────────────────────

class RoomPanelView(discord.ui.View):
    """Persistent view: создание и управление temp-комнатами."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(RoomManageSelect())

    @discord.ui.button(
        label="Создать комнату", style=discord.ButtonStyle.success,
        emoji="🔊", custom_id="room:create", row=0,
    )
    async def create(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        embed = discord.Embed(
            title="Шаг 1 — Тип комнаты",
            description="Выбери тип голосовой комнаты:",
            color=Clr.INFO,
        )
        await interaction.response.send_message(
            embed=embed, view=RoomTypeView(), ephemeral=True,
        )
