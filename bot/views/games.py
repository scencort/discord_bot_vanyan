"""Views: дуэль и свадьба."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from ..core import VoiceSitterBot


class DuelInviteView(discord.ui.View):
    def __init__(self, gid: int, challenger: int, target: int, stake: int) -> None:
        super().__init__(timeout=60)
        self.gid = gid
        self.challenger = challenger
        self.target = target
        self.stake = stake
        self.done = False

    def _lock(self) -> None:
        for c in self.children:
            if isinstance(c, discord.ui.Button):
                c.disabled = True

    @discord.ui.button(label="⚔ Принять", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        if interaction.user.id != self.target:
            await interaction.response.send_message("Только вызванный игрок.", ephemeral=True)
            return
        if self.done:
            await interaction.response.send_message("Дуэль уже завершена.", ephemeral=True)
            return

        bot: VoiceSitterBot = interaction.client  # type: ignore[assignment]
        c = interaction.guild.get_member(self.challenger)
        t = interaction.guild.get_member(self.target)
        if c is None or t is None:
            self.done = True
            self._lock()
            await interaction.response.edit_message(content="Дуэль отменена: участник вышел.", view=self)
            return

        cb = bot.store.balance(self.gid, c.id)
        tb = bot.store.balance(self.gid, t.id)
        if cb < self.stake or tb < self.stake:
            self.done = True
            self._lock()
            await interaction.response.edit_message(content="Дуэль отменена: недостаточно средств.", view=self)
            return

        winner = random.choice([c.id, t.id])
        loser = t.id if winner == c.id else c.id
        try:
            lb, wb = bot.store.transfer(self.gid, loser, winner, self.stake)
        except ValueError:
            self.done = True
            self._lock()
            await interaction.response.edit_message(content="Ошибка перевода.", view=self)
            return

        bot.store.inc_counter(self.gid, c.id, "total_duels")
        bot.store.inc_counter(self.gid, t.id, "total_duels")
        bot.store.inc_counter(self.gid, winner, "duel_wins")

        self.done = True
        self._lock()
        await interaction.response.edit_message(
            content=(
                f"⚔ Победитель: <@{winner}>!\n"
                f"Ставка **{self.stake}** монет переведена от <@{loser}>.\n"
                f"Баланс: <@{winner}> → {wb} · <@{loser}> → {lb}"
            ),
            view=self,
        )

    @discord.ui.button(label="✖ Отклонить", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.target:
            await interaction.response.send_message("Только вызванный игрок.", ephemeral=True)
            return
        if self.done:
            return
        self.done = True
        self._lock()
        await interaction.response.edit_message(content="Дуэль отклонена.", view=self)

    async def on_timeout(self) -> None:
        self.done = True
        self._lock()


class MarryProposalView(discord.ui.View):
    def __init__(self, gid: int, proposer: int, target: int) -> None:
        super().__init__(timeout=120)
        self.gid = gid
        self.proposer = proposer
        self.target = target
        self.done = False

    def _lock(self) -> None:
        for c in self.children:
            if isinstance(c, discord.ui.Button):
                c.disabled = True

    @discord.ui.button(label="💕 Согласиться", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.target:
            await interaction.response.send_message("Ответить может только адресат.", ephemeral=True)
            return
        if self.done:
            return

        bot: VoiceSitterBot = interaction.client  # type: ignore[assignment]
        if bot.store.marriage(self.gid, self.proposer) or bot.store.marriage(self.gid, self.target):
            self.done = True
            self._lock()
            await interaction.response.edit_message(content="Кто-то уже в паре.", view=self)
            return

        if not bot.store.marry(self.gid, self.proposer, self.target):
            self.done = True
            self._lock()
            await interaction.response.edit_message(content="Не удалось создать пару.", view=self)
            return

        self.done = True
        self._lock()
        await interaction.response.edit_message(
            content=f"💕 Новая пара: <@{self.proposer}> и <@{self.target}>. Поздравляю!",
            view=self,
        )

    @discord.ui.button(label="Отказать", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.target:
            await interaction.response.send_message("Ответить может только адресат.", ephemeral=True)
            return
        if self.done:
            return
        self.done = True
        self._lock()
        await interaction.response.edit_message(content="Предложение отклонено.", view=self)

    async def on_timeout(self) -> None:
        self.done = True
        self._lock()
