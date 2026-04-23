"""Cog: модерация — ban, kick, timeout, warn, case info/undo и др."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..config import Clr, utcnow
from ..helpers import (
    apply_warn_thresholds, can_moderate, ensure_admin,
    record_case, set_voice_ban,
)

if TYPE_CHECKING:
    from ..core import VoiceSitterBot


class ModerationCog(commands.Cog):
    def __init__(self, bot: VoiceSitterBot) -> None:
        self.bot = bot

    # ── Базовые команды ──────────────────────────────────────────────

    @app_commands.command(name="say", description="Отправить сообщение от лица бота")
    @app_commands.describe(message="Текст", channel="Канал (опционально)")
    async def say(self, interaction: discord.Interaction, message: str,
                  channel: Optional[discord.TextChannel] = None) -> None:
        if not await ensure_admin(interaction):
            return
        target = channel or interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Нужен текстовый канал.", ephemeral=True)
            return
        await target.send(message)
        await interaction.response.send_message("✅ Отправлено.", ephemeral=True)

    @app_commands.command(name="set_modlog", description="Назначить канал mod-log")
    async def set_modlog(self, interaction: discord.Interaction,
                         channel: discord.TextChannel) -> None:
        if not await ensure_admin(interaction):
            return
        self.bot.store.put(interaction.guild_id, "modlog_channel_id", str(channel.id))
        await interaction.response.send_message(f"✅ Mod-log: {channel.mention}", ephemeral=True)

    @app_commands.command(name="set_alert_channel", description="Канал системных алертов")
    async def set_alert_channel(self, interaction: discord.Interaction,
                                channel: discord.TextChannel) -> None:
        if not await ensure_admin(interaction):
            return
        self.bot.store.put(interaction.guild_id, "alert_channel_id", str(channel.id))
        await interaction.response.send_message(f"✅ Alert: {channel.mention}", ephemeral=True)

    @app_commands.command(name="set_backup_channel", description="Канал для бэкапов")
    async def set_backup_channel(self, interaction: discord.Interaction,
                                 channel: discord.TextChannel) -> None:
        if not await ensure_admin(interaction):
            return
        self.bot.store.put(interaction.guild_id, "backup_channel_id", str(channel.id))
        await interaction.response.send_message(f"✅ Backup: {channel.mention}", ephemeral=True)

    # ── Модерация участников ─────────────────────────────────────────

    @app_commands.command(name="ban", description="Забанить участника")
    @app_commands.describe(member="Участник", reason="Причина", delete_days="Удалить сообщения (0–7)")
    async def ban_cmd(self, interaction: discord.Interaction, member: discord.Member,
                      reason: Optional[str] = None,
                      delete_days: app_commands.Range[int, 0, 7] = 0) -> None:
        if not await ensure_admin(interaction):
            return
        if not isinstance(interaction.user, discord.Member):
            return
        ok, why = can_moderate(interaction.user, member)
        if not ok:
            await interaction.response.send_message(why, ephemeral=True)
            return
        r = reason or "Без причины"
        await interaction.guild.ban(member, reason=r, delete_message_days=delete_days)  # type: ignore[union-attr]
        cid = await record_case(self.bot, interaction.guild, "ban", interaction.user.id,  # type: ignore[arg-type]
                                member.id, r, {"delete_days": delete_days})
        await interaction.response.send_message(f"✅ {member.mention} забанен · Case #{cid}", ephemeral=True)

    @app_commands.command(name="unban", description="Разбанить по ID")
    async def unban_cmd(self, interaction: discord.Interaction, user_id: str,
                        reason: Optional[str] = None) -> None:
        if not await ensure_admin(interaction):
            return
        if interaction.guild is None:
            return
        try:
            uid = int(user_id)
        except ValueError:
            await interaction.response.send_message("user_id — число.", ephemeral=True)
            return
        user = await self.bot.fetch_user(uid)
        r = reason or "Без причины"
        await interaction.guild.unban(user, reason=r)
        cid = await record_case(self.bot, interaction.guild, "unban", interaction.user.id, uid, r)
        await interaction.response.send_message(f"✅ Разбан · Case #{cid}", ephemeral=True)

    @app_commands.command(name="kick", description="Кикнуть участника")
    async def kick_cmd(self, interaction: discord.Interaction, member: discord.Member,
                       reason: Optional[str] = None) -> None:
        if not await ensure_admin(interaction):
            return
        if not isinstance(interaction.user, discord.Member):
            return
        ok, why = can_moderate(interaction.user, member)
        if not ok:
            await interaction.response.send_message(why, ephemeral=True)
            return
        r = reason or "Без причины"
        await member.kick(reason=r)
        cid = await record_case(self.bot, interaction.guild, "kick", interaction.user.id, member.id, r)  # type: ignore[arg-type]
        await interaction.response.send_message(f"✅ {member.mention} кикнут · Case #{cid}", ephemeral=True)

    @app_commands.command(name="timeout", description="Выдать timeout")
    async def timeout_cmd(self, interaction: discord.Interaction, member: discord.Member,
                          minutes: app_commands.Range[int, 1, 40320],
                          reason: Optional[str] = None) -> None:
        if not await ensure_admin(interaction):
            return
        if not isinstance(interaction.user, discord.Member):
            return
        ok, why = can_moderate(interaction.user, member)
        if not ok:
            await interaction.response.send_message(why, ephemeral=True)
            return
        r = reason or "Без причины"
        await member.edit(timed_out_until=utcnow() + timedelta(minutes=minutes), reason=r)
        cid = await record_case(self.bot, interaction.guild, "timeout", interaction.user.id,  # type: ignore[arg-type]
                                member.id, r, {"minutes": minutes})
        await interaction.response.send_message(f"✅ Timeout · Case #{cid}", ephemeral=True)

    @app_commands.command(name="untimeout", description="Снять timeout")
    async def untimeout_cmd(self, interaction: discord.Interaction, member: discord.Member,
                            reason: Optional[str] = None) -> None:
        if not await ensure_admin(interaction):
            return
        if not isinstance(interaction.user, discord.Member):
            return
        ok, why = can_moderate(interaction.user, member)
        if not ok:
            await interaction.response.send_message(why, ephemeral=True)
            return
        r = reason or "Без причины"
        await member.edit(timed_out_until=None, reason=r)
        cid = await record_case(self.bot, interaction.guild, "untimeout", interaction.user.id,  # type: ignore[arg-type]
                                member.id, r)
        await interaction.response.send_message(f"✅ Timeout снят · Case #{cid}", ephemeral=True)

    @app_commands.command(name="voice_ban", description="Запретить voice")
    async def voice_ban_cmd(self, interaction: discord.Interaction, member: discord.Member,
                            reason: Optional[str] = None) -> None:
        if not await ensure_admin(interaction):
            return
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            return
        ok, why = can_moderate(interaction.user, member)
        if not ok:
            await interaction.response.send_message(why, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        r = reason or "Без причины"
        ch = await set_voice_ban(interaction.guild, member, True, r)
        if member.voice:
            await member.move_to(None, reason="voice ban")
        cid = await record_case(self.bot, interaction.guild, "voice_ban", interaction.user.id,
                                member.id, r, {"channels": ch})
        await interaction.followup.send(f"✅ Voice ban · Case #{cid}", ephemeral=True)

    @app_commands.command(name="voice_unban", description="Снять voice ban")
    async def voice_unban_cmd(self, interaction: discord.Interaction, member: discord.Member,
                              reason: Optional[str] = None) -> None:
        if not await ensure_admin(interaction):
            return
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            return
        ok, why = can_moderate(interaction.user, member)
        if not ok:
            await interaction.response.send_message(why, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        r = reason or "Без причины"
        ch = await set_voice_ban(interaction.guild, member, False, r)
        cid = await record_case(self.bot, interaction.guild, "voice_unban", interaction.user.id,
                                member.id, r, {"channels": ch})
        await interaction.followup.send(f"✅ Voice ban снят · Case #{cid}", ephemeral=True)

    @app_commands.command(name="voice_mute", description="Замутить в voice")
    async def voice_mute_cmd(self, interaction: discord.Interaction, member: discord.Member,
                             reason: Optional[str] = None) -> None:
        if not await ensure_admin(interaction):
            return
        if not isinstance(interaction.user, discord.Member):
            return
        ok, why = can_moderate(interaction.user, member)
        if not ok:
            await interaction.response.send_message(why, ephemeral=True)
            return
        r = reason or "Без причины"
        await member.edit(mute=True, reason=r)
        cid = await record_case(self.bot, interaction.guild, "voice_mute", interaction.user.id, member.id, r)  # type: ignore[arg-type]
        await interaction.response.send_message(f"✅ Voice mute · Case #{cid}", ephemeral=True)

    @app_commands.command(name="voice_unmute", description="Снять mute в voice")
    async def voice_unmute_cmd(self, interaction: discord.Interaction, member: discord.Member,
                               reason: Optional[str] = None) -> None:
        if not await ensure_admin(interaction):
            return
        if not isinstance(interaction.user, discord.Member):
            return
        ok, why = can_moderate(interaction.user, member)
        if not ok:
            await interaction.response.send_message(why, ephemeral=True)
            return
        r = reason or "Без причины"
        await member.edit(mute=False, reason=r)
        cid = await record_case(self.bot, interaction.guild, "voice_unmute", interaction.user.id, member.id, r)  # type: ignore[arg-type]
        await interaction.response.send_message(f"✅ Voice unmute · Case #{cid}", ephemeral=True)

    @app_commands.command(name="voice_deafen", description="Заглушить в voice")
    async def voice_deafen_cmd(self, interaction: discord.Interaction, member: discord.Member,
                               reason: Optional[str] = None) -> None:
        if not await ensure_admin(interaction):
            return
        if not isinstance(interaction.user, discord.Member):
            return
        ok, why = can_moderate(interaction.user, member)
        if not ok:
            await interaction.response.send_message(why, ephemeral=True)
            return
        r = reason or "Без причины"
        await member.edit(deafen=True, reason=r)
        cid = await record_case(self.bot, interaction.guild, "voice_deafen", interaction.user.id, member.id, r)  # type: ignore[arg-type]
        await interaction.response.send_message(f"✅ Voice deafen · Case #{cid}", ephemeral=True)

    @app_commands.command(name="voice_undeafen", description="Снять заглушение")
    async def voice_undeafen_cmd(self, interaction: discord.Interaction, member: discord.Member,
                                 reason: Optional[str] = None) -> None:
        if not await ensure_admin(interaction):
            return
        if not isinstance(interaction.user, discord.Member):
            return
        ok, why = can_moderate(interaction.user, member)
        if not ok:
            await interaction.response.send_message(why, ephemeral=True)
            return
        r = reason or "Без причины"
        await member.edit(deafen=False, reason=r)
        cid = await record_case(self.bot, interaction.guild, "voice_undeafen", interaction.user.id, member.id, r)  # type: ignore[arg-type]
        await interaction.response.send_message(f"✅ Voice undeafen · Case #{cid}", ephemeral=True)

    @app_commands.command(name="voice_move", description="Переместить в voice")
    async def voice_move_cmd(self, interaction: discord.Interaction, member: discord.Member,
                             channel: discord.VoiceChannel, reason: Optional[str] = None) -> None:
        if not await ensure_admin(interaction):
            return
        if not isinstance(interaction.user, discord.Member):
            return
        ok, why = can_moderate(interaction.user, member)
        if not ok:
            await interaction.response.send_message(why, ephemeral=True)
            return
        r = reason or "Без причины"
        await member.move_to(channel, reason=r)
        cid = await record_case(self.bot, interaction.guild, "voice_move", interaction.user.id,  # type: ignore[arg-type]
                                member.id, r, {"to": channel.id})
        await interaction.response.send_message(f"✅ Перемещён в {channel.mention} · Case #{cid}", ephemeral=True)

    @app_commands.command(name="clear", description="Удалить сообщения")
    async def clear_cmd(self, interaction: discord.Interaction,
                        amount: app_commands.Range[int, 1, 200],
                        channel: Optional[discord.TextChannel] = None) -> None:
        if not await ensure_admin(interaction):
            return
        target = channel or interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Нужен текстовый канал.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        deleted = await target.purge(limit=amount, reason=f"clear by {interaction.user}")
        cid = await record_case(self.bot, interaction.guild, "clear", interaction.user.id,  # type: ignore[arg-type]
                                None, "Clear", {"count": len(deleted), "channel": target.id})
        await interaction.followup.send(f"✅ Удалено {len(deleted)} · Case #{cid}", ephemeral=True)

    @app_commands.command(name="lock", description="Закрыть канал для @everyone")
    async def lock_cmd(self, interaction: discord.Interaction,
                       channel: Optional[discord.TextChannel] = None,
                       reason: Optional[str] = None) -> None:
        if not await ensure_admin(interaction):
            return
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message("Нужен текстовый канал.", ephemeral=True)
            return
        ow = target.overwrites_for(target.guild.default_role)
        ow.send_messages = False
        await target.set_permissions(target.guild.default_role, overwrite=ow, reason=reason or "lock")
        cid = await record_case(self.bot, interaction.guild, "lock", interaction.user.id,  # type: ignore[arg-type]
                                None, reason or "Без причины", {"channel": target.id})
        await interaction.response.send_message(f"🔒 Закрыт · Case #{cid}", ephemeral=True)

    @app_commands.command(name="unlock", description="Открыть канал для @everyone")
    async def unlock_cmd(self, interaction: discord.Interaction,
                         channel: Optional[discord.TextChannel] = None,
                         reason: Optional[str] = None) -> None:
        if not await ensure_admin(interaction):
            return
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message("Нужен текстовый канал.", ephemeral=True)
            return
        ow = target.overwrites_for(target.guild.default_role)
        ow.send_messages = None
        await target.set_permissions(target.guild.default_role, overwrite=ow, reason=reason or "unlock")
        cid = await record_case(self.bot, interaction.guild, "unlock", interaction.user.id,  # type: ignore[arg-type]
                                None, reason or "Без причины", {"channel": target.id})
        await interaction.response.send_message(f"🔓 Открыт · Case #{cid}", ephemeral=True)

    # ── Case info / undo ─────────────────────────────────────────────

    @app_commands.command(name="case_info", description="Информация по case")
    async def case_info_cmd(self, interaction: discord.Interaction, case_id: int) -> None:
        if not await ensure_admin(interaction):
            return
        row = self.bot.store.get_case(interaction.guild_id, case_id)
        if row is None:
            await interaction.response.send_message("Case не найден.", ephemeral=True)
            return
        embed = discord.Embed(title=f"Case #{case_id}", color=Clr.MOD)
        embed.add_field(name="Действие", value=row["action"], inline=True)
        embed.add_field(name="Модератор", value=f"<@{row['moderator_id']}>" if row["moderator_id"] else "система", inline=True)
        embed.add_field(name="Цель", value=f"<@{row['target_id']}>" if row["target_id"] else "—", inline=True)
        embed.add_field(name="Причина", value=row["reason"] or "—", inline=False)
        embed.add_field(name="Дата", value=row["created_at"], inline=True)
        embed.add_field(name="Отменён", value="да" if row["reverted"] else "нет", inline=True)
        meta = row["metadata"] or "{}"
        if meta != "{}":
            embed.add_field(name="Детали", value=f"```json\n{meta[:900]}\n```", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="case_undo", description="Отменить действие по case ID")
    async def case_undo_cmd(self, interaction: discord.Interaction, case_id: int,
                            reason: Optional[str] = None) -> None:
        if not await ensure_admin(interaction):
            return
        guild = interaction.guild
        if guild is None:
            return
        row = self.bot.store.get_case(guild.id, case_id)
        if row is None:
            await interaction.response.send_message("Case не найден.", ephemeral=True)
            return
        if int(row["reverted"]):
            await interaction.response.send_message("Уже отменён.", ephemeral=True)
            return
        action = str(row["action"])
        tid = row["target_id"]
        if tid is None:
            await interaction.response.send_message("Нет target.", ephemeral=True)
            return
        tm = guild.get_member(int(tid))
        r = reason or f"Undo case #{case_id}"

        try:
            if action in {"timeout", "warn_auto_timeout"}:
                if tm is None:
                    await interaction.response.send_message("Пользователь не найден.", ephemeral=True)
                    return
                await tm.edit(timed_out_until=None, reason=r)
            elif action in {"ban", "warn_auto_ban", "automod_ban"}:
                u = await self.bot.fetch_user(int(tid))
                await guild.unban(u, reason=r)
            elif action in {"voice_ban", "warn_auto_voice_ban", "automod_voice_ban"}:
                if tm is None:
                    await interaction.response.send_message("Пользователь не найден.", ephemeral=True)
                    return
                await set_voice_ban(guild, tm, False, r)
            elif action == "voice_mute":
                if tm is None:
                    await interaction.response.send_message("Пользователь не найден.", ephemeral=True)
                    return
                await tm.edit(mute=False, reason=r)
            elif action == "voice_deafen":
                if tm is None:
                    await interaction.response.send_message("Пользователь не найден.", ephemeral=True)
                    return
                await tm.edit(deafen=False, reason=r)
            else:
                await interaction.response.send_message("Авто-отмена не поддерживается для этого типа.", ephemeral=True)
                return
        except discord.HTTPException:
            await interaction.response.send_message("Discord API отклонил отмену.", ephemeral=True)
            return

        self.bot.store.revert_case(guild.id, case_id)
        new_cid = await record_case(
            self.bot, guild, "case_undo", interaction.user.id, int(tid),
            f"Undo #{case_id}: {r}", {"original_case": case_id, "original_action": action},
        )
        await interaction.response.send_message(f"✅ Case #{case_id} отменён · New #{new_cid}", ephemeral=True)

    # ── Warns ────────────────────────────────────────────────────────

    @app_commands.command(name="warn", description="Выдать warn")
    async def warn_cmd(self, interaction: discord.Interaction, member: discord.Member,
                       points: app_commands.Range[int, 1, 10],
                       reason: Optional[str] = None) -> None:
        if not await ensure_admin(interaction):
            return
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            return
        ok, why = can_moderate(interaction.user, member)
        if not ok:
            await interaction.response.send_message(why, ephemeral=True)
            return
        r = reason or "Без причины"
        s = self.bot.store
        wid = s.add_warn(interaction.guild.id, member.id, interaction.user.id, points, r)
        total = s.warn_total(interaction.guild.id, member.id)
        cid = await record_case(self.bot, interaction.guild, "warn", interaction.user.id, member.id, r,
                                {"warn_id": wid, "points": points, "total": total})
        extras: list[str] = []
        try:
            extras = await apply_warn_thresholds(
                self.bot, interaction.guild, member, total, interaction.user.id, r,
            )
        except discord.HTTPException:
            extras.append("ошибка авто-наказания")
        sfx = f" · авто: {', '.join(extras)}" if extras else ""
        await interaction.response.send_message(
            f"⚠ Warn #{wid} · total={total} · Case #{cid}{sfx}", ephemeral=True,
        )

    @app_commands.command(name="unwarn", description="Снять warn по ID")
    async def unwarn_cmd(self, interaction: discord.Interaction, warn_id: int,
                         reason: Optional[str] = None) -> None:
        if not await ensure_admin(interaction):
            return
        guild = interaction.guild
        if guild is None:
            return
        s = self.bot.store
        row = s.get_warn(guild.id, warn_id)
        if row is None:
            await interaction.response.send_message("Warn не найден.", ephemeral=True)
            return
        if not int(row["active"]):
            await interaction.response.send_message("Warn уже снят.", ephemeral=True)
            return
        uid = int(row["user_id"])
        s.deactivate_warn(guild.id, warn_id)
        total = s.warn_total(guild.id, uid)
        # Пересчёт уровня
        lvl = 0
        if total >= s.get_int(guild.id, "warn_ban_points"):
            lvl = 3
        elif total >= s.get_int(guild.id, "warn_voice_ban_points"):
            lvl = 2
        elif total >= s.get_int(guild.id, "warn_timeout_points"):
            lvl = 1
        s.set_warn_level(guild.id, uid, lvl)
        r = reason or "Без причины"
        cid = await record_case(self.bot, guild, "unwarn", interaction.user.id, uid, r,
                                {"warn_id": warn_id, "new_total": total})
        await interaction.response.send_message(
            f"✅ Warn снят · total={total} · Case #{cid}", ephemeral=True,
        )

    @app_commands.command(name="warns", description="Список warn пользователя")
    async def warns_cmd(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not await ensure_admin(interaction):
            return
        rows = self.bot.store.list_warns(interaction.guild_id, member.id)
        total = self.bot.store.warn_total(interaction.guild_id, member.id)
        if not rows:
            await interaction.response.send_message(f"У {member.mention} нет warn.", ephemeral=True)
            return
        lines = []
        for r in rows:
            st = "✅" if int(r["active"]) else "⬛"
            lines.append(f"{st} #{r['id']} · +{r['points']} · {r['reason'] or '—'}")
        await interaction.response.send_message(
            f"Warns {member.mention} (total={total}):\n" + "\n".join(lines[:20]),
            ephemeral=True,
        )


async def setup(bot: VoiceSitterBot) -> None:
    await bot.add_cog(ModerationCog(bot))
