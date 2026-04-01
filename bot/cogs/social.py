"""Cog: социалка — профиль, жалобы, свадьба, личная роль."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..config import Clr, DAILY_COOLDOWN_H, utcnow
from ..helpers import (
    alert_channel_id, ensure_admin, fmt_remaining, is_owner,
    parse_color, parse_iso, role_manageable,
)

if TYPE_CHECKING:
    from ..core import VoiceSitterBot


class SocialCog(commands.Cog):
    def __init__(self, bot: VoiceSitterBot) -> None:
        self.bot = bot

    def _partner_id(self, gid: int, uid: int) -> Optional[int]:
        row = self.bot.store.marriage(gid, uid)
        if row is None:
            return None
        u1, u2 = int(row["user1_id"]), int(row["user2_id"])
        return u2 if uid == u1 else u1

    # ── /profile ─────────────────────────────────────────────────────

    @app_commands.command(name="profile", description="Информация о пользователе")
    async def profile_cmd(self, interaction: discord.Interaction,
                          member: Optional[discord.Member] = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        target = member or interaction.guild.get_member(interaction.user.id)
        if target is None:
            await interaction.response.send_message("Не найден.", ephemeral=True)
            return

        s = self.bot.store
        gid = interaction.guild.id
        p = s.profile(gid, target.id)
        bal = int(p["balance"])
        streak = int(p["daily_streak"])
        reports = s.report_count(gid, target.id)

        partner = self._partner_id(gid, target.id)
        partner_text = f"<@{partner}>" if partner else "нет"

        prid = s.personal_role(gid, target.id)
        prole = interaction.guild.get_role(prid) if prid else None
        role_text = prole.mention if prole else "нет"

        last = parse_iso(p["daily_last"])
        daily_text = "готово"
        if last is not None:
            nxt = last + timedelta(hours=DAILY_COOLDOWN_H)
            if utcnow() < nxt:
                daily_text = f"через {fmt_remaining(nxt - utcnow())}"

        embed = discord.Embed(
            title=f"Профиль: {target.display_name}",
            color=Clr.SOCIAL, timestamp=utcnow(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Баланс", value=f"{bal} монет", inline=True)
        embed.add_field(name="Streak", value=str(streak), inline=True)
        embed.add_field(name="Пара", value=partner_text, inline=True)
        embed.add_field(name="Личная роль", value=role_text, inline=True)
        embed.add_field(name="Жалоб", value=str(reports), inline=True)
        embed.add_field(name="/timely", value=daily_text, inline=True)
        embed.add_field(
            name="Статистика",
            value=(
                f"Дуэли: {int(p['total_duels'])} · Побед: {int(p['duel_wins'])}\n"
                f"Slots: {int(p['slots_wins'])} · RPS: {int(p['rps_wins'])}"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    # ── /report ──────────────────────────────────────────────────────

    @app_commands.command(name="report", description="Подать жалобу на пользователя")
    @app_commands.describe(member="На кого", reason="Причина")
    async def report_cmd(self, interaction: discord.Interaction,
                         member: discord.Member, reason: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message("Нельзя на себя.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("На ботов нельзя.", ephemeral=True)
            return

        rid = self.bot.store.add_report(
            interaction.guild.id, interaction.user.id, member.id,
            reason.strip()[:500] or "Без причины",
        )

        ch_id = alert_channel_id(self.bot, interaction.guild.id)
        if ch_id:
            ch = interaction.guild.get_channel(ch_id)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                embed = discord.Embed(
                    title=f"Report #{rid}", color=Clr.DANGER, timestamp=utcnow(),
                )
                embed.add_field(name="Reporter", value=f"<@{interaction.user.id}>", inline=True)
                embed.add_field(name="Target", value=f"<@{member.id}>", inline=True)
                embed.add_field(name="Reason", value=reason[:1000], inline=False)
                try:
                    await ch.send(embed=embed)
                except discord.HTTPException:
                    pass

        await interaction.response.send_message(
            f"✅ Жалоба #{rid} отправлена.", ephemeral=True,
        )

    # ── /marry ───────────────────────────────────────────────────────

    @app_commands.command(name="marry", description="Пара: propose / divorce / info")
    @app_commands.describe(member="Пользователь", mode="propose / divorce / info")
    @app_commands.choices(mode=[
        app_commands.Choice(name="propose", value="propose"),
        app_commands.Choice(name="divorce", value="divorce"),
        app_commands.Choice(name="info", value="info"),
    ])
    async def marry_cmd(
        self, interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
        mode: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        sel = mode.value if mode else "propose"
        s = self.bot.store
        gid = interaction.guild.id

        if sel == "info":
            pid = self._partner_id(gid, interaction.user.id)
            if pid is None:
                await interaction.response.send_message("Ты не в паре.", ephemeral=True)
                return
            row = s.marriage(gid, interaction.user.id)
            date = row["created_at"][:10] if row else "?"
            await interaction.response.send_message(
                f"💕 Твоя пара: <@{pid}> (с {date})", ephemeral=True,
            )
            return

        if sel == "divorce":
            if s.divorce(gid, interaction.user.id):
                await interaction.response.send_message("Пара расторгнута.")
            else:
                await interaction.response.send_message("Ты не в паре.", ephemeral=True)
            return

        # propose
        if member is None:
            await interaction.response.send_message("Укажи пользователя.", ephemeral=True)
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message("Нельзя самому себе.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("Ботам нельзя.", ephemeral=True)
            return
        if s.marriage(gid, interaction.user.id):
            await interaction.response.send_message("Ты уже в паре.", ephemeral=True)
            return
        if s.marriage(gid, member.id):
            await interaction.response.send_message("Этот пользователь уже в паре.", ephemeral=True)
            return

        from ..views.games import MarryProposalView
        view = MarryProposalView(gid, interaction.user.id, member.id)
        await interaction.response.send_message(
            f"💕 {member.mention}, тебе предложение от {interaction.user.mention}!",
            view=view,
        )

    # ── /myrole ──────────────────────────────────────────────────────

    @app_commands.command(name="myrole", description="Личная роль: create / rename / color / delete")
    @app_commands.describe(mode="Действие", name="Имя", color_hex="Цвет #RRGGBB")
    @app_commands.choices(mode=[
        app_commands.Choice(name="create", value="create"),
        app_commands.Choice(name="rename", value="rename"),
        app_commands.Choice(name="color", value="color"),
        app_commands.Choice(name="delete", value="delete"),
    ])
    async def myrole_cmd(
        self, interaction: discord.Interaction,
        mode: app_commands.Choice[str],
        name: Optional[str] = None,
        color_hex: Optional[str] = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return

        me = interaction.guild.me
        if me is None or not me.guild_permissions.manage_roles:
            await interaction.response.send_message("Боту нужно Manage Roles.", ephemeral=True)
            return

        s = self.bot.store
        gid = interaction.guild.id
        uid = interaction.user.id
        sel = mode.value

        rid = s.personal_role(gid, uid)
        existing = interaction.guild.get_role(rid) if rid else None

        if sel == "create":
            role_name = (name or f"role-{interaction.user.display_name}").strip()[:100]
            color = parse_color(color_hex) if color_hex else discord.Colour.random()
            if color is None:
                await interaction.response.send_message("Цвет: #RRGGBB.", ephemeral=True)
                return

            if existing is None:
                try:
                    existing = await interaction.guild.create_role(
                        name=role_name, color=color, mentionable=True, reason="myrole create",
                    )
                except discord.HTTPException:
                    await interaction.response.send_message("Не удалось создать роль.", ephemeral=True)
                    return
                s.set_personal_role(gid, uid, existing.id)
            else:
                ok, reason = role_manageable(interaction.guild, existing)
                if not ok:
                    await interaction.response.send_message(reason, ephemeral=True)
                    return
                try:
                    await existing.edit(name=role_name, color=color, reason="myrole update")
                except discord.HTTPException:
                    await interaction.response.send_message("Не удалось обновить.", ephemeral=True)
                    return

            try:
                await interaction.user.add_roles(existing, reason="myrole assign")
            except discord.HTTPException:
                await interaction.response.send_message("Роль создана, но выдать не удалось.", ephemeral=True)
                return
            await interaction.response.send_message(
                f"✅ Личная роль: {existing.mention}", ephemeral=True,
            )
            return

        if existing is None:
            await interaction.response.send_message("Сначала создай роль (mode=create).", ephemeral=True)
            return

        ok, reason = role_manageable(interaction.guild, existing)
        if not ok:
            await interaction.response.send_message(reason, ephemeral=True)
            return

        if sel == "rename":
            if not (name or "").strip():
                await interaction.response.send_message("Укажи имя.", ephemeral=True)
                return
            await existing.edit(name=name.strip()[:100], reason="myrole rename")
            await interaction.response.send_message("✅ Переименовано.", ephemeral=True)

        elif sel == "color":
            if not color_hex:
                await interaction.response.send_message("Укажи цвет #RRGGBB.", ephemeral=True)
                return
            c = parse_color(color_hex)
            if c is None:
                await interaction.response.send_message("Цвет: #RRGGBB.", ephemeral=True)
                return
            await existing.edit(color=c, reason="myrole color")
            await interaction.response.send_message("✅ Цвет обновлён.", ephemeral=True)

        elif sel == "delete":
            try:
                await existing.delete(reason="myrole delete")
            except discord.HTTPException:
                await interaction.response.send_message("Не удалось удалить.", ephemeral=True)
                return
            s.clear_personal_role(gid, uid)
            await interaction.response.send_message("✅ Личная роль удалена.", ephemeral=True)


async def setup(bot: VoiceSitterBot) -> None:
    await bot.add_cog(SocialCog(bot))
