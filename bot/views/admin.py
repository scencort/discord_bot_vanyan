"""Views: Центр управления (admin panel) — категории, модалки, навигация."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING, Optional

import discord

from ..config import Clr, FLOW_TTL_SEC, MOD_ACTIONS, OWNER_IDS, log, utcnow
from ..helpers import (
    execute_mod_action, get_id, is_owner, record_case,
    resolve_one, safe_defer, safe_modal, safe_reply,
)

if TYPE_CHECKING:
    from ..core import VoiceSitterBot


# ═══════════════════════════════════════════════════════════════════════
#  Модалки для действий admin-панели
# ═══════════════════════════════════════════════════════════════════════

class ClearModal(discord.ui.Modal, title="🧹 Очистка сообщений"):
    amount = discord.ui.TextInput(
        label="Количество (1–200)", required=True, default="20", max_length=3,
    )
    channel_id = discord.ui.TextInput(
        label="ID канала (пусто = текущий)", required=False, max_length=22,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not is_owner(interaction.user.id):
            await interaction.response.send_message("Нет доступа.", ephemeral=True)
            return
        bot: VoiceSitterBot = interaction.client  # type: ignore[assignment]
        try:
            n = max(1, min(200, int(self.amount.value.strip())))
        except ValueError:
            await interaction.response.send_message("Число 1–200.", ephemeral=True)
            return

        ch: Optional[discord.abc.Messageable] = interaction.channel
        if (self.channel_id.value or "").strip():
            try:
                ch = interaction.guild.get_channel(int(self.channel_id.value.strip()))
            except ValueError:
                await interaction.response.send_message("ID канала — число.", ephemeral=True)
                return

        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Нужен текстовый канал.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        deleted = await ch.purge(limit=n, reason=f"clear by {interaction.user}")
        cid = await record_case(
            bot, interaction.guild, "clear", interaction.user.id, None,
            "Clear via panel", {"count": len(deleted), "channel_id": ch.id},
        )
        await interaction.followup.send(
            f"✅ Удалено **{len(deleted)}** сообщений · Case #{cid}", ephemeral=True,
        )


class ChannelSettingModal(discord.ui.Modal):
    def __init__(self, key: str, label: str) -> None:
        self.key = key
        super().__init__(title=f"⚙ {label}")
        self.ch_id = discord.ui.TextInput(
            label="ID текстового канала", required=True, max_length=22,
        )
        self.add_item(self.ch_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not is_owner(interaction.user.id):
            await interaction.response.send_message("Нет доступа.", ephemeral=True)
            return
        bot: VoiceSitterBot = interaction.client  # type: ignore[assignment]
        try:
            cid = int(self.ch_id.value.strip())
        except ValueError:
            await interaction.response.send_message("ID — число.", ephemeral=True)
            return
        ch = interaction.guild.get_channel(cid)
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("Текстовый канал не найден.", ephemeral=True)
            return
        bot.store.put(interaction.guild.id, self.key, str(ch.id))
        await interaction.response.send_message(
            f"✅ Настройка обновлена: {ch.mention}", ephemeral=True,
        )


class LockUnlockModal(discord.ui.Modal):
    def __init__(self, lock: bool) -> None:
        self._lock = lock
        super().__init__(title="🔒 Закрыть канал" if lock else "🔓 Открыть канал")
        self.ch_id = discord.ui.TextInput(
            label="ID канала (пусто = текущий)", required=False, max_length=22,
        )
        self.reason_field = discord.ui.TextInput(
            label="Причина", required=False, max_length=300,
        )
        self.add_item(self.ch_id)
        self.add_item(self.reason_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not is_owner(interaction.user.id):
            await interaction.response.send_message("Нет доступа.", ephemeral=True)
            return
        bot: VoiceSitterBot = interaction.client  # type: ignore[assignment]

        target: Optional[discord.abc.GuildChannel] = interaction.channel  # type: ignore[assignment]
        if (self.ch_id.value or "").strip():
            try:
                target = interaction.guild.get_channel(int(self.ch_id.value.strip()))
            except ValueError:
                await interaction.response.send_message("ID — число.", ephemeral=True)
                return
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message("Нужен текстовый канал.", ephemeral=True)
            return

        ow = target.overwrites_for(target.guild.default_role)
        ow.send_messages = False if self._lock else None
        await target.set_permissions(
            target.guild.default_role, overwrite=ow,
            reason=self.reason_field.value or "panel lock/unlock",
        )
        action = "lock" if self._lock else "unlock"
        cid = await record_case(
            bot, interaction.guild, action, interaction.user.id, None,
            self.reason_field.value or "Без причины", {"channel_id": target.id},
        )
        await interaction.response.send_message(
            f"✅ {action.capitalize()} выполнен · Case #{cid}", ephemeral=True,
        )


class ScheduleModal(discord.ui.Modal):
    def __init__(self, action: str) -> None:
        self._action = action
        labels = {
            "schedule_reminder": "⏰ Напоминание",
            "schedule_every": "🔁 Повторяющееся",
            "schedule_remove": "🗑 Удалить расписание",
        }
        super().__init__(title=labels.get(action, action))

        if action in {"schedule_reminder", "schedule_every"}:
            self.v1 = discord.ui.TextInput(label="Через сколько минут", required=True, max_length=6)
            self.v2 = discord.ui.TextInput(
                label="ID канала (пусто = текущий)", required=False, max_length=22,
            )
            self.v3 = discord.ui.TextInput(label="Текст сообщения", required=True, max_length=1500)
            self.add_item(self.v1)
            self.add_item(self.v2)
            self.add_item(self.v3)
        else:
            self.v1 = discord.ui.TextInput(label="ID расписания", required=True, max_length=12)
            self.add_item(self.v1)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not is_owner(interaction.user.id):
            await interaction.response.send_message("Нет доступа.", ephemeral=True)
            return
        bot: VoiceSitterBot = interaction.client  # type: ignore[assignment]

        if self._action == "schedule_remove":
            try:
                sid = int(self.v1.value.strip())
            except ValueError:
                await interaction.response.send_message("ID — число.", ephemeral=True)
                return
            if bot.store.get_schedule(interaction.guild.id, sid) is None:
                await interaction.response.send_message("Не найдено.", ephemeral=True)
                return
            bot.store.remove_schedule(interaction.guild.id, sid)
            await interaction.response.send_message("✅ Расписание удалено.", ephemeral=True)
            return

        try:
            mins = int(self.v1.value.strip())
        except ValueError:
            await interaction.response.send_message("Минуты — число.", ephemeral=True)
            return

        ch: Optional[discord.abc.Messageable] = interaction.channel
        if hasattr(self, "v2") and (self.v2.value or "").strip():
            try:
                ch = interaction.guild.get_channel(int(self.v2.value.strip()))
            except ValueError:
                await interaction.response.send_message("ID канала — число.", ephemeral=True)
                return
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Нужен текстовый канал.", ephemeral=True)
            return

        nxt = int((utcnow() + timedelta(minutes=max(1, mins))).timestamp())
        text = self.v3.value

        if self._action == "schedule_reminder":
            sid = bot.store.add_schedule(
                interaction.guild.id, ch.id, text, nxt, None, interaction.user.id,
            )
            await interaction.response.send_message(
                f"✅ Напоминание создано · ID {sid}", ephemeral=True,
            )
        else:
            interval = int(timedelta(minutes=max(1, mins)).total_seconds())
            sid = bot.store.add_schedule(
                interaction.guild.id, ch.id, text, nxt, interval, interaction.user.id,
            )
            await interaction.response.send_message(
                f"✅ Повторяющееся объявление · ID {sid}", ephemeral=True,
            )


# ═══════════════════════════════════════════════════════════════════════
#  Подменю — эфемерные views по категориям
# ═══════════════════════════════════════════════════════════════════════

class ModerationActionSelect(discord.ui.Select):
    """Выбор модерационного действия (показывается эфемерно)."""

    def __init__(self) -> None:
        options = [
            discord.SelectOption(label="Ban", value="ban", emoji="🛑",
                                 description="Забанить пользователя"),
            discord.SelectOption(label="Kick", value="kick", emoji="👢",
                                 description="Кикнуть пользователя"),
            discord.SelectOption(label="Timeout", value="timeout", emoji="⏱",
                                 description="Выдать timeout"),
            discord.SelectOption(label="Voice Ban", value="voice_ban", emoji="🔇",
                                 description="Запретить voice"),
            discord.SelectOption(label="Warn", value="warn", emoji="⚠",
                                 description="Предупреждение"),
            discord.SelectOption(label="Clear", value="clear", emoji="🧹",
                                 description="Очистить сообщения"),
            discord.SelectOption(label="Lock", value="lock", emoji="🔒",
                                 description="Закрыть канал"),
            discord.SelectOption(label="Unlock", value="unlock", emoji="🔓",
                                 description="Открыть канал"),
            discord.SelectOption(label="Unban", value="unban", emoji="✅",
                                 description="Разбанить"),
            discord.SelectOption(label="Untimeout", value="untimeout", emoji="✅",
                                 description="Снять timeout"),
            discord.SelectOption(label="Voice Unban", value="voice_unban", emoji="🔊",
                                 description="Снять voice ban"),
            discord.SelectOption(label="Unwarn", value="unwarn", emoji="🧹",
                                 description="Снять warn по ID"),
        ]
        super().__init__(
            placeholder="Выбери действие…", options=options,
            custom_id="admin:mod_action",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_owner(interaction.user.id):
            await interaction.response.send_message("Нет доступа.", ephemeral=True)
            return

        action = self.values[0]

        # Модальные действия (clear / lock / unlock)
        if action == "clear":
            await safe_modal(interaction, ClearModal(), ctx="admin:clear")
            return
        if action == "lock":
            await safe_modal(interaction, LockUnlockModal(True), ctx="admin:lock")
            return
        if action == "unlock":
            await safe_modal(interaction, LockUnlockModal(False), ctx="admin:unlock")
            return

        # Модерационные действия → через чат-flow
        if action in MOD_ACTIONS:
            await _start_mod_flow(interaction, action)
            return

        await interaction.response.send_message("Неизвестное действие.", ephemeral=True)


class ModerationSubView(discord.ui.View):
    """Эфемерная панель «Модерация» с выбором действия."""

    def __init__(self) -> None:
        super().__init__(timeout=300)
        self.add_item(ModerationActionSelect())


class SettingsActionSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label="Mod-Log канал", value="modlog_channel_id",
                                 emoji="📝", description="Назначить канал логов"),
            discord.SelectOption(label="Alert канал", value="alert_channel_id",
                                 emoji="🚨", description="Канал алертов"),
            discord.SelectOption(label="Backup канал", value="backup_channel_id",
                                 emoji="🗃", description="Канал резервных копий"),
        ]
        super().__init__(placeholder="Выбери настройку…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_owner(interaction.user.id):
            await interaction.response.send_message("Нет доступа.", ephemeral=True)
            return
        key = self.values[0]
        labels = {
            "modlog_channel_id": "Mod-Log",
            "alert_channel_id": "Alert",
            "backup_channel_id": "Backup",
        }
        await safe_modal(
            interaction,
            ChannelSettingModal(key, labels.get(key, key)),
            ctx=f"admin:set_{key}",
        )


class SettingsSubView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)
        self.add_item(SettingsActionSelect())


class ScheduleActionSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label="Напоминание", value="schedule_reminder",
                                 emoji="⏰", description="Разовое через N минут"),
            discord.SelectOption(label="Повторяющееся", value="schedule_every",
                                 emoji="🔁", description="Каждые N минут"),
            discord.SelectOption(label="Удалить", value="schedule_remove",
                                 emoji="🗑", description="Удалить расписание по ID"),
        ]
        super().__init__(placeholder="Выбери действие…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_owner(interaction.user.id):
            await interaction.response.send_message("Нет доступа.", ephemeral=True)
            return
        await safe_modal(
            interaction, ScheduleModal(self.values[0]),
            ctx=f"admin:{self.values[0]}",
        )


class ScheduleSubView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)
        self.add_item(ScheduleActionSelect())


# ═══════════════════════════════════════════════════════════════════════
#  Главная persistent-панель администратора
# ═══════════════════════════════════════════════════════════════════════

class AdminCategorySelect(discord.ui.Select):
    """Главный select: выбор категории → эфемерный ответ с подменю."""

    def __init__(self) -> None:
        options = [
            discord.SelectOption(label="Модерация", value="moderation",
                                 emoji="🛡", description="Ban, kick, warn, timeout и др."),
            discord.SelectOption(label="Настройки", value="settings",
                                 emoji="⚙", description="Каналы логов, алертов, backup"),
            discord.SelectOption(label="Расписание", value="schedule",
                                 emoji="⏰", description="Таймеры и напоминания"),
        ]
        super().__init__(
            placeholder="📂 Выберите раздел…", options=options,
            custom_id="admin:category",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_owner(interaction.user.id):
            await interaction.response.send_message("Нет доступа.", ephemeral=True)
            return

        cat = self.values[0]

        if cat == "moderation":
            embed = discord.Embed(
                title="🛡 Модерация",
                description=(
                    "Выбери действие ниже.\n\n"
                    "**Для mod-действий** — после выбора напиши в чат:\n"
                    "`@пользователь причина`\n\n"
                    "Или в два шага: сначала цель, потом причину.\n"
                    "Для отмены напиши `отмена`."
                ),
                color=Clr.MOD,
            )
            await interaction.response.send_message(
                embed=embed, view=ModerationSubView(), ephemeral=True,
            )

        elif cat == "settings":
            bot: VoiceSitterBot = interaction.client  # type: ignore[assignment]
            gid = interaction.guild_id or 0
            modlog = get_id(bot, gid, "modlog_channel_id")
            alert = get_id(bot, gid, "alert_channel_id")
            backup = get_id(bot, gid, "backup_channel_id")

            def ch_text(cid: Optional[int]) -> str:
                return f"<#{cid}>" if cid else "не задан"

            embed = discord.Embed(
                title="⚙ Настройки сервера",
                description=(
                    f"📝 Mod-Log: {ch_text(modlog)}\n"
                    f"🚨 Alert: {ch_text(alert)}\n"
                    f"🗃 Backup: {ch_text(backup)}\n\n"
                    "Выбери настройку для изменения:"
                ),
                color=Clr.INFO,
            )
            await interaction.response.send_message(
                embed=embed, view=SettingsSubView(), ephemeral=True,
            )

        elif cat == "schedule":
            embed = discord.Embed(
                title="⏰ Расписание",
                description="Создавай и управляй таймерами и напоминаниями.",
                color=Clr.WARNING,
            )
            await interaction.response.send_message(
                embed=embed, view=ScheduleSubView(), ephemeral=True,
            )


class AdminPanelView(discord.ui.View):
    """
    Persistent view — единая панель Центра управления.
    Публикуется один раз в owner-канале.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(AdminCategorySelect())

    @discord.ui.button(
        label="Backup", style=discord.ButtonStyle.secondary,
        emoji="💾", custom_id="admin:backup", row=1,
    )
    async def backup(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not is_owner(interaction.user.id):
            await interaction.response.send_message("Нет доступа.", ephemeral=True)
            return
        from ..cogs.system import build_backup_file
        f = await build_backup_file(interaction.client, interaction.guild)  # type: ignore[arg-type]
        await interaction.response.send_message("💾 Backup:", file=f, ephemeral=True)

    @discord.ui.button(
        label="Sync", style=discord.ButtonStyle.success,
        emoji="⚡", custom_id="admin:sync", row=1,
    )
    async def sync(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not is_owner(interaction.user.id):
            await interaction.response.send_message("Нет доступа.", ephemeral=True)
            return
        bot: VoiceSitterBot = interaction.client  # type: ignore[assignment]
        bot._commands_synced = False
        await bot.sync_commands()
        await interaction.response.send_message("⚡ Команды синхронизированы.", ephemeral=True)

    @discord.ui.button(
        label="Обновить панели", style=discord.ButtonStyle.secondary,
        emoji="🔄", custom_id="admin:republish", row=1,
    )
    async def republish(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not is_owner(interaction.user.id):
            await interaction.response.send_message("Нет доступа.", ephemeral=True)
            return
        from ..cogs.setup import publish_panels
        await publish_panels(interaction.client, interaction.guild)  # type: ignore[arg-type]
        await interaction.response.send_message("🔄 Панели обновлены.", ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════
#  Chat-flow модерации (вызывается из admin-панели)
# ═══════════════════════════════════════════════════════════════════════

async def _start_mod_flow(interaction: discord.Interaction, action: str) -> None:
    """Запуск chat-flow модерации: после выбора action пользователь пишет цель+причину."""
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Только на сервере.", ephemeral=True)
        return

    bot: VoiceSitterBot = interaction.client  # type: ignore[assignment]
    admin_ch = get_id(bot, interaction.guild.id, "owner_admin_channel_id")
    if admin_ch and interaction.channel_id != admin_ch:
        await interaction.response.send_message(
            f"Используй в owner-канале: <#{admin_ch}>.", ephemeral=True,
        )
        return

    key = (interaction.guild.id, interaction.user.id)
    bot.pending_actions[key] = {
        "action": action,
        "stage": "await_target",
        "channel_id": interaction.channel_id,
        "created_at": utcnow(),
    }

    if action == "unwarn":
        hint = "`ID_warn причина`"
    elif action == "unban":
        hint = "`@пользователь причина`"
    else:
        hint = "`@пользователь причина`"

    embed = discord.Embed(
        title=f"🛡 {action.upper()} — введите данные",
        description=(
            f"Напишите в чат: {hint}\n\n"
            "Или в 2 шага:\n"
            "1️⃣ Сначала цель\n"
            "2️⃣ Потом причина\n\n"
            "Для отмены: `отмена`"
        ),
        color=Clr.MOD,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def handle_mod_flow(message: discord.Message) -> bool:
    """Обработка сообщения в рамках chat-flow модерации. Возвращает True если обработано."""
    if message.guild is None or not isinstance(message.author, discord.Member):
        return False
    if not is_owner(message.author.id):
        return False

    bot: VoiceSitterBot = message.guild._state._get_client()  # type: ignore[attr-defined]
    key = (message.guild.id, message.author.id)
    state = bot.pending_actions.get(key)
    if state is None:
        return False

    ch_id = state.get("channel_id")
    if isinstance(ch_id, int) and message.channel.id != ch_id:
        return False

    created = state.get("created_at")
    if isinstance(created, __import__("datetime").datetime):
        if (utcnow() - created).total_seconds() > FLOW_TTL_SEC:
            bot.pending_actions.pop(key, None)
            await message.reply("⏰ Сессия истекла. Выбери действие заново.", mention_author=False)
            return True

    action = str(state.get("action", ""))
    stage = str(state.get("stage", "await_target"))
    text = message.content.strip()

    async def finish(reason_text: str) -> bool:
        target_input = str(state.get("target_input", "")).strip()
        prefetched = state.get("target_member_id")
        if not target_input:
            bot.pending_actions.pop(key, None)
            await message.reply("⚠ Цель не зафиксирована.", mention_author=False)
            return True

        reason = "Без причины" if reason_text in {"-", "—"} else (reason_text or "Без причины")
        try:
            result = await execute_mod_action(
                bot, message.guild, message.author, action, target_input, reason,
                prefetched_id=prefetched if isinstance(prefetched, int) else None,
            )
            await message.reply(result, mention_author=False)
        except ValueError as e:
            await message.reply(f"❌ {e}", mention_author=False)
        except discord.HTTPException:
            await message.reply(
                "❌ Ошибка Discord API. Проверь права бота.", mention_author=False,
            )
        finally:
            bot.pending_actions.pop(key, None)
        return True

    if text.casefold() in {"отмена", "cancel"}:
        bot.pending_actions.pop(key, None)
        await message.reply("Отменено.", mention_author=False)
        return True

    if stage == "await_target":
        if action == "unwarn":
            m = re.match(r"\s*(\d{1,20})(?:\s+(.+))?\s*$", text)
            if m is None:
                await message.reply("Нужен числовой ID warn.", mention_author=False)
                return True
            state["target_input"] = m.group(1)
            inline = (m.group(2) or "").strip()
            if inline:
                return await finish(inline)
        else:
            mention = message.mentions[0] if message.mentions else None
            if mention:
                state["target_input"] = f"<@{mention.id}>"
                if isinstance(mention, discord.Member):
                    state["target_member_id"] = mention.id
                inline = re.sub(r"^<@!?\d+>\s*", "", text).strip()
                if inline:
                    return await finish(inline)
            else:
                if not text:
                    await message.reply("Укажи пользователя.", mention_author=False)
                    return True
                id_m = re.match(r"\s*(\d{5,20})(?:\s+(.+))?\s*$", text)
                if id_m:
                    state["target_input"] = id_m.group(1)
                    inline = (id_m.group(2) or "").strip()
                    if inline:
                        return await finish(inline)
                else:
                    state["target_input"] = text

        state["stage"] = "await_reason"
        await message.reply(
            "✅ Цель принята. Теперь напиши причину (или `-` без причины).",
            mention_author=False,
        )
        return True

    if stage == "await_reason":
        return await finish(text)

    bot.pending_actions.pop(key, None)
    return False
