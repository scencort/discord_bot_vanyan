"""Cog: экономика — баланс, daily, перевод, магазин, топ."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..config import (
    Clr, DAILY_BASE_REWARD, DAILY_COOLDOWN_H,
    DAILY_STREAK_BONUS, DAILY_STREAK_CAP, iso_now, utcnow,
)
from ..helpers import ensure_admin, fmt_remaining, parse_iso, role_manageable

if TYPE_CHECKING:
    from ..core import VoiceSitterBot


class EconomyCog(commands.Cog):
    def __init__(self, bot: VoiceSitterBot) -> None:
        self.bot = bot

    # ── /balance ─────────────────────────────────────────────────────

    @app_commands.command(name="balance", description="Проверить баланс")
    async def balance_cmd(self, interaction: discord.Interaction,
                          member: Optional[discord.Member] = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        target = member or (interaction.guild.get_member(interaction.user.id) or interaction.user)
        coins = self.bot.store.balance(interaction.guild.id, target.id)
        await interaction.response.send_message(
            f"Баланс {target.mention}: **{coins}** монет",
        )

    # ── /timely ──────────────────────────────────────────────────────

    @app_commands.command(name="timely", description="Ежедневная награда")
    async def timely_cmd(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        uid = interaction.user.id
        gid = interaction.guild.id
        s = self.bot.store
        p = s.profile(gid, uid)
        now = utcnow()
        last = parse_iso(p["daily_last"])

        if last is not None:
            nxt = last + timedelta(hours=DAILY_COOLDOWN_H)
            if now < nxt:
                await interaction.response.send_message(
                    f"Следующая награда через {fmt_remaining(nxt - now)}.",
                    ephemeral=True,
                )
                return

        old_streak = int(p["daily_streak"])
        if last is None:
            streak = 1
        elif now - last <= timedelta(hours=DAILY_COOLDOWN_H * 2):
            streak = old_streak + 1
        else:
            streak = 1

        reward = DAILY_BASE_REWARD + min(streak - 1, DAILY_STREAK_CAP) * DAILY_STREAK_BONUS
        new_bal = s.add_balance(gid, uid, reward)
        s.set_daily(gid, uid, iso_now(), streak)
        await interaction.response.send_message(
            f"✅ Daily: **+{reward}** монет · Баланс: {new_bal} · Streak: {streak}",
        )

    # ── /give ────────────────────────────────────────────────────────

    @app_commands.command(name="give", description="Перевести монеты")
    async def give_cmd(self, interaction: discord.Interaction,
                       member: discord.Member,
                       amount: app_commands.Range[int, 1, 1_000_000]) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message("Нельзя переводить самому себе.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("Ботам переводы запрещены.", ephemeral=True)
            return
        try:
            fb, tb = self.bot.store.transfer(interaction.guild.id, interaction.user.id, member.id, amount)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ Перевод **{amount}** монет → {member.mention}\n"
            f"Твой баланс: {fb} · Его: {tb}",
        )

    # ── /top ─────────────────────────────────────────────────────────

    @app_commands.command(name="top", description="Топ по балансу")
    async def top_cmd(self, interaction: discord.Interaction,
                      limit: app_commands.Range[int, 3, 20] = 10) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        rows = self.bot.store.top_profiles(interaction.guild.id, limit)
        if not rows:
            await interaction.response.send_message("Пока нет данных.", ephemeral=True)
            return
        lines: list[str] = []
        for i, r in enumerate(rows, 1):
            uid = int(r["user_id"])
            m = interaction.guild.get_member(uid)
            name = m.display_name if m else f"user:{uid}"
            lines.append(f"**{i}.** {name} — {int(r['balance'])} монет")
        embed = discord.Embed(
            title="🏆 Топ по балансу", description="\n".join(lines), color=Clr.ECONOMY,
        )
        await interaction.response.send_message(embed=embed)

    # ── /shop ────────────────────────────────────────────────────────

    @app_commands.command(name="shop", description="Магазин ролей сервера")
    @app_commands.describe(mode="browse / buy / add / remove", role="Роль", price="Цена для add")
    @app_commands.choices(mode=[
        app_commands.Choice(name="browse", value="browse"),
        app_commands.Choice(name="buy", value="buy"),
        app_commands.Choice(name="add", value="add"),
        app_commands.Choice(name="remove", value="remove"),
    ])
    async def shop_cmd(
        self, interaction: discord.Interaction,
        mode: app_commands.Choice[str],
        role: Optional[discord.Role] = None,
        price: Optional[app_commands.Range[int, 1, 1_000_000]] = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return

        s = self.bot.store
        sel = mode.value

        if sel == "browse":
            rows = s.list_shop(interaction.guild.id)
            if not rows:
                await interaction.response.send_message("Магазин пуст.", ephemeral=True)
                return
            lines: list[str] = []
            for r in rows[:25]:
                sr = interaction.guild.get_role(int(r["role_id"]))
                if sr:
                    lines.append(f"{sr.mention} — {int(r['price'])} монет")
            if not lines:
                await interaction.response.send_message("Нет доступных ролей.", ephemeral=True)
                return
            embed = discord.Embed(
                title="🛒 Магазин ролей", description="\n".join(lines), color=Clr.ECONOMY,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if sel == "buy":
            if role is None:
                await interaction.response.send_message("Укажи роль для покупки.", ephemeral=True)
                return
            row = s.shop_role(interaction.guild.id, role.id)
            if row is None:
                await interaction.response.send_message("Роли нет в магазине.", ephemeral=True)
                return
            if role in interaction.user.roles:
                await interaction.response.send_message("У тебя уже есть эта роль.", ephemeral=True)
                return
            ok, reason = role_manageable(interaction.guild, role)
            if not ok:
                await interaction.response.send_message(reason, ephemeral=True)
                return
            cost = int(row["price"])
            try:
                new_bal = s.add_balance(interaction.guild.id, interaction.user.id, -cost)
            except ValueError:
                await interaction.response.send_message("Недостаточно монет.", ephemeral=True)
                return
            try:
                await interaction.user.add_roles(role, reason="Shop purchase")
            except discord.HTTPException:
                s.add_balance(interaction.guild.id, interaction.user.id, cost)
                await interaction.response.send_message("Не удалось выдать роль. Монеты возвращены.", ephemeral=True)
                return
            await interaction.response.send_message(
                f"✅ Куплено: {role.mention} · −{cost} · Баланс: {new_bal}", ephemeral=True,
            )
            return

        if sel in {"add", "remove"}:
            if not await ensure_admin(interaction):
                return

        if sel == "add":
            if role is None or price is None:
                await interaction.response.send_message("Укажи role и price.", ephemeral=True)
                return
            ok, reason = role_manageable(interaction.guild, role)
            if not ok:
                await interaction.response.send_message(reason, ephemeral=True)
                return
            s.upsert_shop_role(interaction.guild.id, role.id, price)
            await interaction.response.send_message(
                f"✅ {role.mention} в магазине за {price} монет.", ephemeral=True,
            )
            return

        if sel == "remove":
            if role is None:
                await interaction.response.send_message("Укажи роль.", ephemeral=True)
                return
            if s.remove_shop_role(interaction.guild.id, role.id):
                await interaction.response.send_message(
                    f"✅ {role.mention} удалена из магазина.", ephemeral=True,
                )
            else:
                await interaction.response.send_message("Роли нет в магазине.", ephemeral=True)
            return

        await interaction.response.send_message("Неизвестный режим.", ephemeral=True)


async def setup(bot: VoiceSitterBot) -> None:
    await bot.add_cog(EconomyCog(bot))
