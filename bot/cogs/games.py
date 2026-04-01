"""Cog: игры — дуэль, слоты, RPS, музыка."""

from __future__ import annotations

import random
import shutil
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..config import Clr, VOICE_CHANNEL_ID
from ..views.games import DuelInviteView

if TYPE_CHECKING:
    from ..core import VoiceSitterBot


class GamesCog(commands.Cog):
    def __init__(self, bot: VoiceSitterBot) -> None:
        self.bot = bot

    # ── /duel ────────────────────────────────────────────────────────

    @app_commands.command(name="duel", description="Вызвать на дуэль")
    @app_commands.describe(member="Кого вызываешь", stake="Ставка")
    async def duel_cmd(self, interaction: discord.Interaction,
                       member: discord.Member,
                       stake: app_commands.Range[int, 10, 1_000_000] = 50) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message("Нельзя с самим собой.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("Нельзя с ботом.", ephemeral=True)
            return

        s = self.bot.store
        gid = interaction.guild.id
        if s.balance(gid, interaction.user.id) < stake:
            await interaction.response.send_message("Не хватает монет.", ephemeral=True)
            return
        if s.balance(gid, member.id) < stake:
            await interaction.response.send_message("У соперника мало монет.", ephemeral=True)
            return

        view = DuelInviteView(gid, interaction.user.id, member.id, stake)
        await interaction.response.send_message(
            f"⚔ {member.mention}, вызов от {interaction.user.mention}! Ставка: **{stake}** монет.",
            view=view,
        )

    # ── /slots ───────────────────────────────────────────────────────

    @app_commands.command(name="slots", description="Слоты")
    async def slots_cmd(self, interaction: discord.Interaction,
                        bet: app_commands.Range[int, 10, 1_000_000] = 50) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return

        symbols = ["🍒", "🍋", "🔔", "💎", "7️⃣"]
        rolled = [random.choice(symbols) for _ in range(3)]
        line = " ".join(rolled)

        s = self.bot.store
        gid = interaction.guild.id
        uid = interaction.user.id

        try:
            s.add_balance(gid, uid, -bet)
        except ValueError:
            await interaction.response.send_message("Не хватает монет.", ephemeral=True)
            return

        payout = 0
        if rolled[0] == rolled[1] == rolled[2]:
            if rolled[0] == "7️⃣":
                payout = bet * 8
            elif rolled[0] == "💎":
                payout = bet * 5
            else:
                payout = bet * 3
        elif rolled[0] == rolled[1] or rolled[1] == rolled[2] or rolled[0] == rolled[2]:
            payout = int(bet * 1.8)

        if payout > 0:
            bal = s.add_balance(gid, uid, payout)
            if payout > bet:
                s.inc_counter(gid, uid, "slots_wins")
            result = f"Выигрыш: **+{payout - bet}**"
        else:
            bal = s.balance(gid, uid)
            result = f"Проигрыш: **−{bet}**"

        await interaction.response.send_message(
            f"🎰 {line}\n{result} · Баланс: {bal}",
        )

    # ── /rps ─────────────────────────────────────────────────────────

    @app_commands.command(name="rps", description="Камень-ножницы-бумага")
    @app_commands.choices(choice=[
        app_commands.Choice(name="камень", value="rock"),
        app_commands.Choice(name="ножницы", value="scissors"),
        app_commands.Choice(name="бумага", value="paper"),
    ])
    async def rps_cmd(self, interaction: discord.Interaction,
                      choice: app_commands.Choice[str]) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return

        bot_choice = random.choice(["rock", "paper", "scissors"])
        icons = {"rock": "🪨", "paper": "📄", "scissors": "✂"}
        player = choice.value
        s = self.bot.store
        gid = interaction.guild.id
        uid = interaction.user.id

        line = f"Ты {icons[player]} vs бот {icons[bot_choice]}"

        if player == bot_choice:
            await interaction.response.send_message(f"🤝 Ничья: {line}")
            return

        wins = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
        if wins[player] == bot_choice:
            bal = s.add_balance(gid, uid, 30)
            s.inc_counter(gid, uid, "rps_wins")
            await interaction.response.send_message(f"🎉 Победа: {line} · +30 · Баланс: {bal}")
        else:
            cur = s.balance(gid, uid)
            pen = min(10, cur)
            if pen > 0:
                bal = s.add_balance(gid, uid, -pen)
            else:
                bal = cur
            await interaction.response.send_message(f"😔 Поражение: {line} · −{pen} · Баланс: {bal}")

    # ── /play ────────────────────────────────────────────────────────

    @app_commands.command(name="play", description="Воспроизвести аудио в voice")
    @app_commands.describe(url="Прямая ссылка на аудио")
    async def play_cmd(self, interaction: discord.Interaction, url: str) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        if not url.startswith(("http://", "https://")):
            await interaction.response.send_message("Нужна http/https ссылка.", ephemeral=True)
            return
        if shutil.which("ffmpeg") is None:
            await interaction.response.send_message("ffmpeg не найден.", ephemeral=True)
            return
        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.response.send_message("Зайди в voice.", ephemeral=True)
            return
        if interaction.user.voice.channel.id != VOICE_CHANNEL_ID:
            await interaction.response.send_message(
                f"Зайди в <#{VOICE_CHANNEL_ID}> для /play.", ephemeral=True,
            )
            return

        target = interaction.guild.get_channel(VOICE_CHANNEL_ID)
        if not isinstance(target, (discord.VoiceChannel, discord.StageChannel)):
            await interaction.response.send_message("Voice-канал не найден.", ephemeral=True)
            return

        vc = discord.utils.get(self.bot.voice_clients, guild=interaction.guild)
        try:
            if vc is None or not vc.is_connected():
                vc = await target.connect(reconnect=True, self_deaf=False, self_mute=False)
            elif vc.channel and vc.channel.id != target.id:
                await vc.move_to(target)
        except discord.HTTPException:
            await interaction.response.send_message("Не удалось подключиться.", ephemeral=True)
            return

        try:
            if vc.is_playing():
                vc.stop()
            source = discord.FFmpegPCMAudio(
                url,
                before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                options="-vn",
            )
            vc.play(source)
        except Exception:
            await interaction.response.send_message("Не удалось воспроизвести. Проверь ссылку.", ephemeral=True)
            return

        await interaction.response.send_message("🎵 Музыка запущена.")


async def setup(bot: VoiceSitterBot) -> None:
    await bot.add_cog(GamesCog(bot))
