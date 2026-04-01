"""Общие утилиты — проверки прав, резолв пользователей, safe-обёртки."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands

from .config import OWNER_IDS, Clr, log, utcnow

if TYPE_CHECKING:
    from .core import VoiceSitterBot


# ── Маленькие parse/format утилиты ───────────────────────────────────

def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def fmt_remaining(delta: timedelta) -> str:
    total = max(0, int(delta.total_seconds()))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ч {m}м"
    if m:
        return f"{m}м {s}с"
    return f"{s}с"


def parse_color(raw: str) -> Optional[discord.Colour]:
    text = raw.strip().lower().replace("#", "")
    if not re.fullmatch(r"[0-9a-f]{6}", text):
        return None
    return discord.Colour(int(text, 16))


def parse_domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower().replace("www.", "")


# ── Проверки прав ────────────────────────────────────────────────────

def is_owner(uid: int) -> bool:
    return uid in OWNER_IDS


def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator


def can_admin(interaction: discord.Interaction) -> bool:
    if is_owner(interaction.user.id):
        return True
    if isinstance(interaction.user, discord.Member):
        return is_admin(interaction.user)
    return False


async def ensure_admin(interaction: discord.Interaction) -> bool:
    if can_admin(interaction):
        return True
    await interaction.response.send_message(
        "Нужны права администратора сервера.", ephemeral=True,
    )
    return False


def can_bot_moderate(target: discord.Member) -> tuple[bool, str]:
    me = target.guild.me
    if me is None:
        return False, "Не удалось получить бота на сервере."
    if target.id == target.guild.owner_id:
        return False, "Нельзя модерировать владельца сервера."
    if target.top_role >= me.top_role:
        return False, "Роль бота ниже или равна роли цели."
    return True, ""


def can_moderate(mod: discord.Member, target: discord.Member) -> tuple[bool, str]:
    if mod.id == target.id:
        return False, "Нельзя модерировать самого себя."
    if target.id == target.guild.owner_id:
        return False, "Нельзя модерировать владельца сервера."
    if mod.id != target.guild.owner_id and target.top_role >= mod.top_role:
        return False, "У цели роль выше или равна твоей."
    ok, reason = can_bot_moderate(target)
    if not ok:
        return False, reason
    return True, ""


def role_manageable(guild: discord.Guild, role: discord.Role) -> tuple[bool, str]:
    me = guild.me
    if me is None:
        return False, "Не удалось получить бота на сервере."
    if role.managed:
        return False, "Роль управляется интеграцией."
    if role >= me.top_role:
        return False, "Роль выше или равна роли бота."
    return True, ""


# ── Резолв пользователей ────────────────────────────────────────────

def _norm_token(tok: str) -> str:
    t = tok.strip()
    if t.startswith("<@") and t.endswith(">"):
        t = t.strip("<@!>")
    if t.startswith("@"):
        t = t[1:]
    return t.strip()


async def resolve_members(guild: discord.Guild, raw: str) -> tuple[list[discord.Member], list[str]]:
    found: list[discord.Member] = []
    missed: list[str] = []
    for token in [s.strip() for s in raw.replace(";", ",").split(",") if s.strip()]:
        norm = _norm_token(token)
        member: Optional[discord.Member] = None

        id_m = re.search(r"\d{5,20}", norm)
        if id_m:
            mid = int(id_m.group(0))
            member = guild.get_member(mid)
            if member is None:
                try:
                    member = await guild.fetch_member(mid)
                except discord.HTTPException:
                    pass
        else:
            low = norm.casefold()
            member = discord.utils.find(
                lambda m: m.name.casefold() == low or m.display_name.casefold() == low,
                guild.members,
            )
            if member is None:
                partial = [m for m in guild.members
                           if low in m.display_name.casefold() or low in m.name.casefold()]
                if len(partial) == 1:
                    member = partial[0]

        if member is None:
            missed.append(token)
        elif member not in found:
            found.append(member)
    return found, missed


async def resolve_one(guild: discord.Guild, raw: str) -> tuple[Optional[discord.Member], Optional[str]]:
    members, missed = await resolve_members(guild, raw)
    if members:
        return members[0], None
    return None, f"Не удалось найти: {missed[0] if missed else raw}"


async def resolve_banned(guild: discord.Guild, raw: str, client: discord.Client) -> tuple[Optional[discord.User], Optional[str]]:
    norm = _norm_token(raw)
    id_m = re.search(r"\d{5,20}", norm)
    if id_m:
        try:
            return await client.fetch_user(int(id_m.group(0))), None
        except discord.HTTPException:
            return None, "Не удалось получить пользователя по ID."

    low = norm.casefold()
    try:
        async for entry in guild.bans(limit=1000):
            u = entry.user
            uname = u.name.casefold() if u.name else ""
            gname = u.global_name.casefold() if u.global_name else ""
            if low in {uname, gname} or (low and (low in uname or low in gname)):
                return u, None
    except discord.HTTPException:
        return None, "Не удалось прочитать бан-лист."
    return None, f"'{raw}' не найден в бан-листе."


# ── Настройки — удобные обёртки ──────────────────────────────────────

def get_id(bot: VoiceSitterBot, gid: int, key: str) -> Optional[int]:
    v = bot.store.get(gid, key)
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def alert_channel_id(bot: VoiceSitterBot, gid: int) -> Optional[int]:
    return get_id(bot, gid, "alert_channel_id") or get_id(bot, gid, "modlog_channel_id")


# ── Алерты и логирование ─────────────────────────────────────────────

async def send_alert(bot: VoiceSitterBot, guild: discord.Guild, text: str) -> None:
    ch_id = alert_channel_id(bot, guild.id)
    if not ch_id:
        return
    ch = guild.get_channel(ch_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(ch_id)
        except discord.HTTPException:
            return
    if isinstance(ch, (discord.TextChannel, discord.Thread)):
        try:
            await ch.send(f"🚨 {text}")
        except discord.HTTPException:
            pass


async def record_case(
    bot: VoiceSitterBot,
    guild: discord.Guild,
    action: str,
    mod_id: Optional[int],
    target_id: Optional[int],
    reason: str,
    meta: Optional[dict] = None,
) -> int:
    cid = bot.store.add_case(guild.id, action, mod_id, target_id, reason, meta)

    modlog_id = get_id(bot, guild.id, "modlog_channel_id")
    if not modlog_id:
        return cid

    ch = guild.get_channel(modlog_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(modlog_id)
        except discord.HTTPException:
            return cid
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return cid

    embed = discord.Embed(
        title=f"Case #{cid}: {action}", color=Clr.MOD, timestamp=utcnow(),
    )
    embed.add_field(name="Модератор", value=f"<@{mod_id}>" if mod_id else "система", inline=True)
    embed.add_field(name="Цель", value=f"<@{target_id}>" if target_id else "—", inline=True)
    embed.add_field(name="Причина", value=reason or "Без причины", inline=False)
    if meta:
        pretty = json.dumps(meta, ensure_ascii=False)[:900]
        embed.add_field(name="Детали", value=f"```json\n{pretty}\n```", inline=False)
    try:
        await ch.send(embed=embed)
    except discord.HTTPException:
        pass
    return cid


# ── Модерационные действия ───────────────────────────────────────────

async def set_voice_ban(guild: discord.Guild, target: discord.Member,
                        enabled: bool, reason: str) -> int:
    changed = 0
    for ch in guild.channels:
        if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
            continue
        ow = ch.overwrites_for(target)
        if enabled:
            ow.connect = False
            ow.speak = False
            ow.stream = False
            ow.use_soundboard = False
        else:
            ow.connect = None
            ow.speak = None
            ow.stream = None
            ow.use_soundboard = None
        if ow.is_empty():
            await ch.set_permissions(target, overwrite=None, reason=reason)
        else:
            await ch.set_permissions(target, overwrite=ow, reason=reason)
        changed += 1
    return changed


async def apply_warn_thresholds(
    bot: VoiceSitterBot,
    guild: discord.Guild,
    target: discord.Member,
    total: int,
    mod_id: int,
    reason: str,
) -> list[str]:
    s = bot.store
    tp = s.get_int(guild.id, "warn_timeout_points")
    vp = s.get_int(guild.id, "warn_voice_ban_points")
    bp = s.get_int(guild.id, "warn_ban_points")
    tm = s.get_int(guild.id, "warn_timeout_minutes")
    old_level = s.get_warn_level(guild.id, target.id)

    new_level = 0
    if total >= bp:
        new_level = 3
    elif total >= vp:
        new_level = 2
    elif total >= tp:
        new_level = 1

    if new_level <= old_level:
        return []

    actions: list[str] = []
    if new_level == 1:
        until = utcnow() + timedelta(minutes=tm)
        await target.edit(timed_out_until=until, reason=f"Warn threshold: {reason}")
        cid = await record_case(bot, guild, "warn_auto_timeout", mod_id, target.id,
                                f"Авто-timeout ({tm} мин)", {"total_points": total})
        actions.append(f"timeout ({tm} мин, case #{cid})")
    if new_level == 2:
        await set_voice_ban(guild, target, True, f"Warn threshold: {reason}")
        if target.voice:
            await target.move_to(None, reason="Warn threshold voice ban")
        cid = await record_case(bot, guild, "warn_auto_voice_ban", mod_id, target.id,
                                "Авто voice ban", {"total_points": total})
        actions.append(f"voice_ban (case #{cid})")
    if new_level == 3:
        await guild.ban(target, reason=f"Warn threshold: {reason}", delete_message_days=0)
        cid = await record_case(bot, guild, "warn_auto_ban", mod_id, target.id,
                                "Авто ban", {"total_points": total})
        actions.append(f"ban (case #{cid})")

    s.set_warn_level(guild.id, target.id, new_level)
    return actions


async def apply_auto_timeout(
    bot: VoiceSitterBot, guild: discord.Guild, target: discord.Member,
    minutes: int, reason: str, action_name: str,
    meta: Optional[dict] = None,
) -> bool:
    ok, why = can_bot_moderate(target)
    if not ok:
        await send_alert(bot, guild, f"Не удалось наказать <@{target.id}>: {why}")
        return False
    try:
        await target.edit(timed_out_until=utcnow() + timedelta(minutes=minutes), reason=reason)
    except discord.HTTPException:
        return False
    await record_case(bot, guild, action_name, None, target.id, reason, meta)
    return True


# ── Общий исполнитель модерации (admin-панель / chat-flow) ───────────

async def execute_mod_action(
    bot: VoiceSitterBot,
    guild: discord.Guild,
    moderator: discord.Member,
    action: str,
    target_input: str,
    reason: str,
    *,
    value_raw: str = "",
    prefetched_id: Optional[int] = None,
) -> str:
    nr = (reason or "Без причины").strip() or "Без причины"
    nv = (value_raw or "").strip()
    target: Optional[discord.Member] = None
    target_id: Optional[int] = None
    banned_user: Optional[discord.User] = None

    if action == "unwarn":
        try:
            target_id = int(target_input.strip())
        except ValueError as e:
            raise ValueError("Нужен числовой ID warn.") from e
    elif action == "unban":
        banned_user, err = await resolve_banned(guild, target_input, bot)
        if banned_user is None:
            raise ValueError(err or "Не найден в бан-листе.")
        target_id = banned_user.id
    else:
        if prefetched_id is not None:
            target = guild.get_member(prefetched_id)
            if target is None:
                try:
                    target = await guild.fetch_member(prefetched_id)
                except discord.HTTPException:
                    pass
        if target is None:
            target, err = await resolve_one(guild, target_input)
            if target is None:
                raise ValueError(err or "Пользователь не найден.")
        ok, why = can_moderate(moderator, target)
        if not ok:
            raise ValueError(why)
        target_id = target.id

    if action == "ban":
        dd = int(nv) if nv else 0
        dd = max(0, min(7, dd))
        await guild.ban(target, reason=nr, delete_message_days=dd)  # type: ignore[arg-type]
        cid = await record_case(bot, guild, "ban", moderator.id, target.id, nr, {"delete_days": dd})  # type: ignore[union-attr]
        return f"✅ Ban выполнен · Case #{cid}"

    if action == "kick":
        await target.kick(reason=nr)  # type: ignore[union-attr]
        cid = await record_case(bot, guild, "kick", moderator.id, target.id, nr)  # type: ignore[union-attr]
        return f"✅ Kick выполнен · Case #{cid}"

    if action == "timeout":
        mins = int(nv) if nv else 60
        mins = max(1, mins)
        await target.edit(timed_out_until=utcnow() + timedelta(minutes=mins), reason=nr)  # type: ignore[union-attr]
        cid = await record_case(bot, guild, "timeout", moderator.id, target.id, nr, {"minutes": mins})  # type: ignore[union-attr]
        return f"✅ Timeout выполнен · Case #{cid}"

    if action == "voice_ban":
        await set_voice_ban(guild, target, True, nr)  # type: ignore[arg-type]
        if target.voice:  # type: ignore[union-attr]
            await target.move_to(None, reason="voice ban")  # type: ignore[union-attr]
        cid = await record_case(bot, guild, "voice_ban", moderator.id, target.id, nr)  # type: ignore[union-attr]
        return f"✅ Voice ban выполнен · Case #{cid}"

    if action == "warn":
        pts = int(nv) if nv else 1
        pts = max(1, pts)
        wid = bot.store.add_warn(guild.id, target.id, moderator.id, pts, nr)  # type: ignore[union-attr]
        total = bot.store.warn_total(guild.id, target.id)  # type: ignore[union-attr]
        cid = await record_case(bot, guild, "warn", moderator.id, target.id, nr,  # type: ignore[union-attr]
                                {"warn_id": wid, "points": pts, "total_points": total})
        extras = await apply_warn_thresholds(bot, guild, target, total, moderator.id, nr)  # type: ignore[arg-type]
        sfx = f" · авто: {', '.join(extras)}" if extras else ""
        return f"✅ Warn выполнен · Case #{cid}{sfx}"

    if action == "unban":
        await guild.unban(banned_user, reason=nr)  # type: ignore[arg-type]
        cid = await record_case(bot, guild, "unban", moderator.id, target_id, nr)
        return f"✅ Unban выполнен · Case #{cid}"

    if action == "untimeout":
        await target.edit(timed_out_until=None, reason=nr)  # type: ignore[union-attr]
        cid = await record_case(bot, guild, "untimeout", moderator.id, target.id, nr)  # type: ignore[union-attr]
        return f"✅ Untimeout выполнен · Case #{cid}"

    if action == "voice_unban":
        await set_voice_ban(guild, target, False, nr)  # type: ignore[arg-type]
        cid = await record_case(bot, guild, "voice_unban", moderator.id, target.id, nr)  # type: ignore[union-attr]
        return f"✅ Voice unban выполнен · Case #{cid}"

    if action == "unwarn":
        row = bot.store.get_warn(guild.id, target_id)  # type: ignore[arg-type]
        if row is None:
            raise ValueError("Warn ID не найден.")
        if int(row["active"]) == 0:
            raise ValueError("Warn уже снят.")
        bot.store.deactivate_warn(guild.id, target_id)  # type: ignore[arg-type]
        cid = await record_case(bot, guild, "unwarn", moderator.id, int(row["user_id"]), nr,
                                {"warn_id": target_id})
        return f"✅ Unwarn выполнен · Case #{cid}"

    raise ValueError("Неизвестное действие.")


# ── Safe-обёртки для interaction (обработка 10062/expiry) ────────────

async def safe_modal(interaction: discord.Interaction, modal: discord.ui.Modal,
                     *, ctx: str, delay: float = 0.8) -> bool:
    for attempt in range(2):
        try:
            await interaction.response.send_modal(modal)
            return True
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError) as e:
            if attempt == 0:
                log.warning("Сетевой сбой modal (%s), повтор: %s", ctx, e)
                await asyncio.sleep(delay)
                continue
            log.warning("Modal fail после повтора (%s): %s", ctx, e)
            break
        except discord.HTTPException as e:
            log.warning("HTTP ошибка modal (%s): %s", ctx, e)
            break
        except Exception:
            log.exception("Неожиданная ошибка modal (%s)", ctx)
            break

    text = "Не удалось открыть окно. Попробуй ещё раз через пару секунд."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except Exception:
        pass
    return False


async def safe_defer(interaction: discord.Interaction, *, ephemeral: bool = True,
                     thinking: bool = True, ctx: str = "") -> bool:
    try:
        if interaction.response.is_done():
            return True
        await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)
        return True
    except discord.NotFound:
        log.warning("Interaction истёк до defer (%s)", ctx)
        return False
    except (aiohttp.ClientError, asyncio.TimeoutError, discord.HTTPException) as e:
        log.warning("Defer fail (%s): %s", ctx, e)
        return False


async def safe_reply(interaction: discord.Interaction, text: str, *,
                     ephemeral: bool = True, ctx: str = "",
                     embed: Optional[discord.Embed] = None,
                     view: Optional[discord.ui.View] = None,
                     file: Optional[discord.File] = None) -> bool:
    kwargs: dict = {"ephemeral": ephemeral}
    if embed:
        kwargs["embed"] = embed
    if view:
        kwargs["view"] = view
    if file:
        kwargs["file"] = file
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, **kwargs)
        else:
            await interaction.response.send_message(text, **kwargs)
        return True
    except discord.NotFound:
        log.warning("Interaction истёк до send (%s)", ctx)
        return False
    except (aiohttp.ClientError, asyncio.TimeoutError, discord.HTTPException) as e:
        log.warning("Reply fail (%s): %s", ctx, e)
        return False


# ── Вспомогательные функции audit-лога ───────────────────────────────

async def fetch_audit_executor(
    guild: discord.Guild, action: discord.AuditLogAction, target_id: int,
) -> Optional[discord.Member]:
    try:
        async for entry in guild.audit_logs(limit=8, action=action):
            if entry.target is None or getattr(entry.target, "id", None) != target_id:
                continue
            if (utcnow() - entry.created_at).total_seconds() > 20:
                continue
            if entry.user is None:
                return None
            return guild.get_member(entry.user.id)
    except discord.HTTPException:
        pass
    return None
