<<<<<<< ours
<<<<<<< ours
<<<<<<< ours
"""Cog: AutoMod + Anti-raid."""
=======
"""Cog: Anti-raid + Anti-nuke."""
>>>>>>> theirs
=======
"""Cog: Anti-raid + Anti-nuke."""
>>>>>>> theirs
=======
"""Cog: Anti-raid + Anti-nuke."""
>>>>>>> theirs

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta
<<<<<<< ours
<<<<<<< ours
<<<<<<< ours
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse
=======
from typing import TYPE_CHECKING
>>>>>>> theirs
=======
from typing import TYPE_CHECKING
>>>>>>> theirs
=======
from typing import TYPE_CHECKING
>>>>>>> theirs

import discord
from discord import app_commands
from discord.ext import commands

<<<<<<< ours
<<<<<<< ours
<<<<<<< ours
from ..config import DEFAULTS_INT, URL_RE, Clr, utcnow
from ..helpers import (
    apply_auto_timeout, can_bot_moderate, ensure_admin,
    fetch_audit_executor, is_admin, is_owner, record_case,
    send_alert, set_voice_ban,
=======
=======
>>>>>>> theirs
=======
>>>>>>> theirs
from ..config import utcnow
from ..helpers import (
    apply_auto_timeout,
    ensure_admin,
    fetch_audit_executor,
    is_admin,
    is_owner,
    send_alert,
<<<<<<< ours
<<<<<<< ours
>>>>>>> theirs
=======
>>>>>>> theirs
=======
>>>>>>> theirs
)

if TYPE_CHECKING:
    from ..core import VoiceSitterBot


class AutoModCog(commands.Cog):
<<<<<<< ours
<<<<<<< ours
<<<<<<< ours
=======
    """Historical name kept for extension compatibility."""

>>>>>>> theirs
=======
    """Historical name kept for extension compatibility."""

>>>>>>> theirs
=======
    """Historical name kept for extension compatibility."""

>>>>>>> theirs
    def __init__(self, bot: VoiceSitterBot) -> None:
        self.bot = bot
        self.join_windows: dict[int, deque[datetime]] = defaultdict(deque)
        self.channel_windows: dict[int, deque[datetime]] = defaultdict(deque)
        self.role_windows: dict[int, deque[datetime]] = defaultdict(deque)
<<<<<<< ours
<<<<<<< ours
<<<<<<< ours
        self.msg_windows: dict[tuple[int, int], deque[datetime]] = defaultdict(deque)

    # ── Внешние методы (вызываются из Bot.on_message) ────────────────
=======
>>>>>>> theirs
=======
>>>>>>> theirs
=======
>>>>>>> theirs

    async def check_mass_mentions(self, message: discord.Message) -> bool:
        if message.guild is None or not isinstance(message.author, discord.Member):
            return False
        if message.author.bot or is_owner(message.author.id) or is_admin(message.author):
            return False

        s = self.bot.store
        limit = s.get_int(message.guild.id, "raid_mention_limit")
        count = len(message.mentions) + len(message.role_mentions)
        if message.mention_everyone:
            count += 4
        if count < limit:
            return False

        try:
            await message.delete()
        except discord.HTTPException:
            pass

        await apply_auto_timeout(
<<<<<<< ours
<<<<<<< ours
<<<<<<< ours
            self.bot, message.guild, message.author, 20,
            "Anti-raid: mass mention", "anti_raid_mass_mention",
            {"mentions": count},
        )
        await send_alert(
            self.bot, message.guild,
=======
=======
>>>>>>> theirs
=======
>>>>>>> theirs
            self.bot,
            message.guild,
            message.author,
            20,
            "Anti-raid: mass mention",
            "anti_raid_mass_mention",
            {"mentions": count},
        )
        await send_alert(
            self.bot,
            message.guild,
<<<<<<< ours
<<<<<<< ours
>>>>>>> theirs
=======
>>>>>>> theirs
=======
>>>>>>> theirs
            f"Mass mention: <@{message.author.id}> ({count} упоминаний). Timeout.",
        )
        return True

<<<<<<< ours
<<<<<<< ours
<<<<<<< ours
    async def check_automod(self, message: discord.Message) -> None:
        if message.guild is None or not isinstance(message.author, discord.Member):
            return
        if message.author.bot:
            return
        member = message.author
        if self._is_exempt(member, message.channel.id):
            return
        content = message.content.strip()
        if not content:
            return

        gid = message.guild.id
        now = utcnow()
        s = self.bot.store

        # Спам
        key = (gid, member.id)
        w = self.msg_windows[key]
        win_sec = s.get_int(gid, "automod_spam_window_sec")
        max_msgs = s.get_int(gid, "automod_spam_messages")
        w.append(now)
        cutoff = now - timedelta(seconds=win_sec)
        while w and w[0] < cutoff:
            w.popleft()
        if len(w) >= max_msgs:
            await self._violation(message, "spam", f"{len(w)} msgs / {win_sec}s")
            return

        # Caps
        letters = [c for c in content if c.isalpha()]
        min_len = s.get_int(gid, "automod_caps_min_len")
        if len(letters) >= min_len:
            upper = sum(1 for c in letters if c.isupper())
            ratio = int(upper / max(len(letters), 1) * 100)
            if ratio >= s.get_int(gid, "automod_caps_percent"):
                await self._violation(message, "caps", f"{ratio}% uppercase")
                return

        # Плохие слова
        bad = [w.casefold() for w in s.get_csv(gid, "automod_bad_words")]
        low = content.casefold()
        for word in bad:
            if word and word in low:
                await self._violation(message, "blacklisted_word", word)
                return

        # Ссылки
        if s.get_int(gid, "automod_block_links") == 1:
            wl = set(s.get_csv(gid, "automod_whitelist_domains"))
            for url in URL_RE.findall(content):
                domain = (urlparse(url).netloc or "").lower().replace("www.", "")
                if domain and domain not in wl:
                    await self._violation(message, "link", domain)
                    return

    # ── Внутренние утилиты ───────────────────────────────────────────

    def _is_exempt(self, member: discord.Member, ch_id: int) -> bool:
        if is_owner(member.id) or is_admin(member):
            return True
        s = self.bot.store
        gid = member.guild.id
        if ch_id in s.get_id_set(gid, "automod_exempt_channel_ids"):
            return True
        if s.get_id_set(gid, "automod_exempt_role_ids") & {r.id for r in member.roles}:
            return True
        return False

    async def _violation(self, msg: discord.Message, kind: str, detail: str) -> None:
        guild = msg.guild
        member = msg.author
        if guild is None or not isinstance(member, discord.Member):
            return

        try:
            await msg.delete()
        except discord.HTTPException:
            pass

        s = self.bot.store
        offense = s.inc_offense(guild.id, member.id)

        if offense == 1:
            wid = s.add_warn(guild.id, member.id, None, 1, f"AutoMod: {kind}")
            cid = await record_case(
                self.bot, guild, "automod_warn", None, member.id,
                f"AutoMod: {kind}", {"detail": detail, "offense": offense, "warn_id": wid},
            )
            await send_alert(self.bot, guild, f"AutoMod warn: <@{member.id}> · {kind} · case #{cid}")
        elif offense == 2:
            await apply_auto_timeout(
                self.bot, guild, member, 10,
                f"AutoMod ({kind})", "automod_timeout",
                {"detail": detail, "offense": offense},
            )
            await send_alert(self.bot, guild, f"AutoMod timeout: <@{member.id}> · {kind}")
        elif offense == 3:
            ok, why = can_bot_moderate(member)
            if ok:
                await set_voice_ban(guild, member, True, f"AutoMod ({kind})")
                if member.voice:
                    await member.move_to(None, reason="AutoMod voice ban")
                cid = await record_case(
                    self.bot, guild, "automod_voice_ban", None, member.id,
                    f"AutoMod ({kind})", {"detail": detail, "offense": offense},
                )
                await send_alert(self.bot, guild, f"AutoMod voice_ban: <@{member.id}> · case #{cid}")
        else:
            ok, why = can_bot_moderate(member)
            if ok:
                try:
                    await guild.ban(member, reason=f"AutoMod ({kind})", delete_message_days=0)
                    cid = await record_case(
                        self.bot, guild, "automod_ban", None, member.id,
                        f"AutoMod ({kind})", {"detail": detail, "offense": offense},
                    )
                    await send_alert(self.bot, guild, f"AutoMod ban: <@{member.id}> · case #{cid}")
                except discord.HTTPException:
                    pass

        try:
            await msg.channel.send(
                f"<@{member.id}> AutoMod: {kind}.", delete_after=8,
            )
        except discord.HTTPException:
            pass

    # ── Anti-raid события ────────────────────────────────────────────

=======
>>>>>>> theirs
=======
>>>>>>> theirs
=======
>>>>>>> theirs
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        guild = member.guild
        now = utcnow()
        w = self.join_windows[guild.id]
        w.append(now)
        cutoff = now - timedelta(minutes=1)
        while w and w[0] < cutoff:
            w.popleft()
        limit = self.bot.store.get_int(guild.id, "raid_join_limit")
        if len(w) <= limit:
            return
        await apply_auto_timeout(
<<<<<<< ours
<<<<<<< ours
<<<<<<< ours
            self.bot, guild, member, 30,
            "Anti-raid: join spike", "anti_raid_join",
            {"joins": len(w), "limit": limit},
        )
        await send_alert(
            self.bot, guild,
=======
=======
>>>>>>> theirs
=======
>>>>>>> theirs
            self.bot,
            guild,
            member,
            30,
            "Anti-raid: join spike",
            "anti_raid_join",
            {"joins": len(w), "limit": limit},
        )
        await send_alert(
            self.bot,
            guild,
<<<<<<< ours
<<<<<<< ours
>>>>>>> theirs
=======
>>>>>>> theirs
=======
>>>>>>> theirs
            f"Join spike: {len(w)}/мин (лимит {limit}). <@{member.id}> timeout.",
        )

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        guild = channel.guild
        now = utcnow()
        w = self.channel_windows[guild.id]
        w.append(now)
        cutoff = now - timedelta(minutes=1)
        while w and w[0] < cutoff:
            w.popleft()
        limit = self.bot.store.get_int(guild.id, "raid_channel_create_limit")
        if len(w) <= limit:
            return

        offender = await fetch_audit_executor(guild, discord.AuditLogAction.channel_create, channel.id)
        if offender:
            await apply_auto_timeout(
<<<<<<< ours
<<<<<<< ours
<<<<<<< ours
                self.bot, guild, offender, 30,
                "Anti-nuke: channel spike", "anti_nuke_channel_create",
=======
=======
>>>>>>> theirs
=======
>>>>>>> theirs
                self.bot,
                guild,
                offender,
                30,
                "Anti-nuke: channel spike",
                "anti_nuke_channel_create",
<<<<<<< ours
<<<<<<< ours
>>>>>>> theirs
=======
>>>>>>> theirs
=======
>>>>>>> theirs
                {"count": len(w), "limit": limit},
            )
        try:
            await channel.delete(reason="Anti-nuke: auto cleanup")
        except discord.HTTPException:
            pass
        oid = offender.id if offender else None
        text = f"Anti-nuke: каналы ({len(w)}/мин)."
        if oid:
            text += f" Offender: <@{oid}>"
        await send_alert(self.bot, guild, text)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        guild = role.guild
        now = utcnow()
        w = self.role_windows[guild.id]
        w.append(now)
        cutoff = now - timedelta(minutes=1)
        while w and w[0] < cutoff:
            w.popleft()
        limit = self.bot.store.get_int(guild.id, "raid_role_create_limit")
        if len(w) <= limit:
            return

        offender = await fetch_audit_executor(guild, discord.AuditLogAction.role_create, role.id)
        if offender:
            await apply_auto_timeout(
<<<<<<< ours
<<<<<<< ours
<<<<<<< ours
                self.bot, guild, offender, 30,
                "Anti-nuke: role spike", "anti_nuke_role_create",
=======
=======
>>>>>>> theirs
=======
>>>>>>> theirs
                self.bot,
                guild,
                offender,
                30,
                "Anti-nuke: role spike",
                "anti_nuke_role_create",
<<<<<<< ours
<<<<<<< ours
>>>>>>> theirs
=======
>>>>>>> theirs
=======
>>>>>>> theirs
                {"count": len(w), "limit": limit},
            )
        try:
            await role.delete(reason="Anti-nuke: auto cleanup")
        except discord.HTTPException:
            pass
        oid = offender.id if offender else None
        text = f"Anti-nuke: роли ({len(w)}/мин)."
        if oid:
            text += f" Offender: <@{oid}>"
        await send_alert(self.bot, guild, text)

<<<<<<< ours
<<<<<<< ours
<<<<<<< ours
    # ── Настройки AutoMod / Anti-raid (slash-команды) ────────────────

    @app_commands.command(name="set_raid_limits", description="Лимиты anti-raid")
    @app_commands.describe(
        join_per_minute="Лимит входов/мин", channel_create="Каналов/мин",
        role_create="Ролей/мин", mention_limit="Упоминаний в сообщении",
    )
    async def set_raid_limits(
        self, interaction: discord.Interaction,
=======
=======
>>>>>>> theirs
=======
>>>>>>> theirs
    @app_commands.command(name="set_raid_limits", description="Лимиты anti-raid")
    @app_commands.describe(
        join_per_minute="Лимит входов/мин",
        channel_create="Каналов/мин",
        role_create="Ролей/мин",
        mention_limit="Упоминаний в сообщении",
    )
    async def set_raid_limits(
        self,
        interaction: discord.Interaction,
<<<<<<< ours
<<<<<<< ours
>>>>>>> theirs
=======
>>>>>>> theirs
=======
>>>>>>> theirs
        join_per_minute: app_commands.Range[int, 3, 50],
        channel_create: app_commands.Range[int, 1, 30],
        role_create: app_commands.Range[int, 1, 20],
        mention_limit: app_commands.Range[int, 3, 30],
    ) -> None:
        if not await ensure_admin(interaction):
            return
        gid = interaction.guild_id
        s = self.bot.store
        s.put(gid, "raid_join_limit", str(join_per_minute))
        s.put(gid, "raid_channel_create_limit", str(channel_create))
        s.put(gid, "raid_role_create_limit", str(role_create))
        s.put(gid, "raid_mention_limit", str(mention_limit))
        await interaction.response.send_message("✅ Лимиты обновлены.", ephemeral=True)

<<<<<<< ours
<<<<<<< ours
<<<<<<< ours
    @app_commands.command(name="set_automod_words", description="Запрещённые слова (через запятую)")
    async def set_automod_words(self, interaction: discord.Interaction, words: str) -> None:
        if not await ensure_admin(interaction):
            return
        norm = [w.strip().casefold() for w in words.split(",") if w.strip()]
        self.bot.store.put_csv(interaction.guild_id, "automod_bad_words", norm)
        await interaction.response.send_message(f"✅ Слов: {len(norm)}", ephemeral=True)

    @app_commands.command(name="set_automod_whitelist", description="Whitelist доменов")
    async def set_automod_whitelist(self, interaction: discord.Interaction, domains: str) -> None:
        if not await ensure_admin(interaction):
            return
        norm = [d.strip().lower().replace("www.", "") for d in domains.split(",") if d.strip()]
        self.bot.store.put_csv(interaction.guild_id, "automod_whitelist_domains", norm)
        await interaction.response.send_message(f"✅ Доменов: {len(norm)}", ephemeral=True)

    @app_commands.command(name="set_automod_exempt_channel", description="Exempt-канал AutoMod")
    @app_commands.describe(mode="add/remove")
    @app_commands.choices(mode=[
        app_commands.Choice(name="add", value="add"),
        app_commands.Choice(name="remove", value="remove"),
    ])
    async def exempt_ch(self, interaction: discord.Interaction,
                        channel: discord.TextChannel,
                        mode: app_commands.Choice[str]) -> None:
        if not await ensure_admin(interaction):
            return
        s = self.bot.store
        vals = s.get_id_set(interaction.guild_id, "automod_exempt_channel_ids")
        if mode.value == "add":
            vals.add(channel.id)
        else:
            vals.discard(channel.id)
        s.put_id_set(interaction.guild_id, "automod_exempt_channel_ids", vals)
        await interaction.response.send_message("✅ Обновлено.", ephemeral=True)

    @app_commands.command(name="set_automod_exempt_role", description="Exempt-роль AutoMod")
    @app_commands.describe(mode="add/remove")
    @app_commands.choices(mode=[
        app_commands.Choice(name="add", value="add"),
        app_commands.Choice(name="remove", value="remove"),
    ])
    async def exempt_role(self, interaction: discord.Interaction,
                          role: discord.Role,
                          mode: app_commands.Choice[str]) -> None:
        if not await ensure_admin(interaction):
            return
        s = self.bot.store
        vals = s.get_id_set(interaction.guild_id, "automod_exempt_role_ids")
        if mode.value == "add":
            vals.add(role.id)
        else:
            vals.discard(role.id)
        s.put_id_set(interaction.guild_id, "automod_exempt_role_ids", vals)
        await interaction.response.send_message("✅ Обновлено.", ephemeral=True)

=======
>>>>>>> theirs
=======
>>>>>>> theirs
=======
>>>>>>> theirs

async def setup(bot: VoiceSitterBot) -> None:
    await bot.add_cog(AutoModCog(bot))
